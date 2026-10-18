"""Open-file error messages and the memory budget."""

from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import server
from engine import reader as reader_mod
from engine.reader import RadarReader, max_open_bytes, system_memory_bytes, tree_memory_bytes
from tests.conftest import make_synthetic_datatree
from tests.test_reader import _write_generic_netcdf
from tests.test_server import _make_handler, _reader_with_tree

KTLX = Path.home() / "Downloads/Others/XRADAR_EXAMPLES/KTLX20130520_201643_V06.gz"


@pytest.fixture(autouse=True)
def _server_state():
    saved = (dict(server._file_readers), server._last_file_id)
    server._file_readers.clear()
    yield
    server._file_readers.clear()
    server._file_readers.update(saved[0])
    server._last_file_id = saved[1]


def _open_error(path: Path) -> str:
    with pytest.raises(Exception) as exc:
        RadarReader().open_file(str(path))
    return f"{type(exc.value).__name__}: {exc.value}"


class TestOpenErrors:
    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.nc"
        f.write_bytes(b"")
        text = _open_error(f)
        assert "empty" in text
        assert "empty.nc" in text

    def test_text_file_named_like_radar(self, tmp_path: Path) -> None:
        f = tmp_path / "notes.nc"
        f.write_text("this is just some notes about radar\n" * 5)
        text = _open_error(f)
        assert "Could not open notes.nc" in text
        assert "plain-text" in text
        assert "NEXRAD" in text  # lists what is supported

    def test_html_error_page(self, tmp_path: Path) -> None:
        f = tmp_path / "KTLX20130520_201643_V06"
        f.write_text("<!DOCTYPE html><html><body>403 Forbidden</body></html>")
        text = _open_error(f)
        assert "web page" in text
        assert "download" in text

    def test_binary_garbage_is_unrecognised(self, tmp_path: Path) -> None:
        f = tmp_path / "blob.dat"
        f.write_bytes(bytes(range(256)) * 8)
        text = _open_error(f)
        assert "not recognised" in text
        assert "NEXRAD" in text

    def test_truncated_netcdf(self, tmp_path: Path) -> None:
        good = tmp_path / "ok.nc"
        _write_generic_netcdf(good)
        data = good.read_bytes()
        bad = tmp_path / "cut.nc"
        bad.write_bytes(data[: len(data) // 2])
        text = _open_error(bad)
        assert "Could not open cut.nc" in text
        assert "truncated or corrupt" in text

    def test_truncated_gzip(self, tmp_path: Path) -> None:
        payload = os.urandom(200_000)
        full = tmp_path / "full.gz"
        full.write_bytes(gzip.compress(payload))
        cut = tmp_path / "cut.gz"
        cut.write_bytes(full.read_bytes()[:50_000])
        text = _open_error(cut)
        assert text.startswith("ValueError")
        assert "gzip archive is truncated or corrupt" in text
        assert "cut.gz" in text

    def test_truncated_gzip_leaves_no_temp_file(self, tmp_path: Path) -> None:
        cut = tmp_path / "cut.gz"
        cut.write_bytes(gzip.compress(os.urandom(100_000))[:20_000])
        before = set(Path(reader_mod.tempfile.gettempdir()).glob("tmp*_cut"))
        _open_error(cut)
        assert set(Path(reader_mod.tempfile.gettempdir()).glob("tmp*_cut")) == before

    def test_corrupt_gzip_body(self, tmp_path: Path) -> None:
        raw = bytearray(gzip.compress(os.urandom(100_000)))
        raw[500:520] = b"\x00" * 20
        f = tmp_path / "bad.gz"
        f.write_bytes(bytes(raw))
        assert "corrupt" in _open_error(f)

    @pytest.mark.skipif(not KTLX.exists(), reason="KTLX sample not available")
    def test_truncated_real_nexrad_gzip(self, tmp_path: Path) -> None:
        cut = tmp_path / "KTLX_cut.gz"
        cut.write_bytes(KTLX.read_bytes()[:2_000_000])
        text = _open_error(cut)
        assert "KTLX_cut.gz" in text
        assert "truncated or corrupt" in text

    @pytest.mark.skipif(not KTLX.exists(), reason="KTLX sample not available")
    def test_truncated_real_nexrad_plain(self, tmp_path: Path) -> None:
        raw = gzip.decompress(KTLX.read_bytes())
        cut = tmp_path / "KTLX_cut_V06"
        cut.write_bytes(raw[:3_000_000])
        # xradar may still decode the sweeps that are complete; either way, no raw traceback.
        try:
            RadarReader().open_file(str(cut))
        except ValueError as exc:
            assert "KTLX_cut_V06" in str(exc)

    @pytest.mark.skipif(
        sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="needs POSIX permissions and a non-root user",
    )
    def test_permission_denied(self, tmp_path: Path) -> None:
        f = tmp_path / "locked.nc"
        _write_generic_netcdf(f)
        f.chmod(0)
        try:
            text = _open_error(f)
        finally:
            f.chmod(0o600)
        assert text.startswith("PermissionError")
        assert "locked.nc" in text
        assert "permissions" in text


class TestOpenErrorsServer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("make", "needle"),
        [
            (lambda p: None, "File not found"),
            (lambda p: p.write_bytes(b""), "empty"),
            (lambda p: p.write_text("hello radar " * 10), "plain-text"),
        ],
    )
    async def test_messages_are_prefixed_and_specific(
        self,
        tmp_path: Path,
        make,
        needle,
    ) -> None:
        f = tmp_path / "x.nc"
        make(f)
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "open_file", "path": str(f)}))
        errors = [m for m in handler._sent if m["type"] == "error"]
        assert len(errors) == 1
        msg = errors[0]["message"]
        # the UI attributes errors to a handler by this prefix (App.svelte isOpenFileError)
        assert msg.startswith("open_file failed: ")
        assert needle in msg
        assert "Traceback" not in msg
        assert not any(m["type"] == "file_opened" for m in handler._sent)

    @pytest.mark.asyncio
    async def test_truncated_gzip_message(self, tmp_path: Path) -> None:
        cut = tmp_path / "cut.gz"
        cut.write_bytes(gzip.compress(os.urandom(100_000))[:20_000])
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "open_file", "path": str(cut)}))
        msg = next(m for m in handler._sent if m["type"] == "error")["message"]
        assert "truncated or corrupt" in msg
        assert msg.startswith("open_file failed: ")

    def test_describe_open_error_variants(self) -> None:
        d = server._describe_open_error
        assert "Permission denied" in d("/a/b.nc", PermissionError(13, "denied"))
        assert "Not enough memory" in d("/a/b.nc", MemoryError())
        assert "folder" in d("/a/b", IsADirectoryError())
        assert "Could not read b.nc" in d("/a/b.nc", OSError(5, "Input/output error"))
        assert "RuntimeError" in d("/a/b.nc", RuntimeError("boom"))


class TestMemoryBudget:
    def test_env_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XRADAR_MAX_OPEN_MB", "100")
        assert max_open_bytes() == 100 * 1024 * 1024
        monkeypatch.setenv("XRADAR_MAX_OPEN_MB", "0")
        assert max_open_bytes() is None
        monkeypatch.setenv("XRADAR_MAX_OPEN_MB", "not-a-number")
        total = system_memory_bytes()
        assert max_open_bytes() == (int(total * 0.6) if total else None)

    def test_default_is_60_percent_of_ram(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XRADAR_MAX_OPEN_MB", raising=False)
        total = system_memory_bytes()
        assert total is None or total > 256 * 1024 * 1024
        if total:
            assert max_open_bytes() == int(total * 0.6)

    def test_reader_memory_counts_tree(self) -> None:
        tree = make_synthetic_datatree(n_azimuth=20, n_range=30)
        reader = _reader_with_tree(tree)
        expected = sum(
            int(v.nbytes) for node in tree.subtree for v in node.to_dataset().variables.values()
        )
        assert tree_memory_bytes(tree) == expected
        assert reader.memory_bytes() == expected > 0
        assert RadarReader().memory_bytes() == 0

    def test_reader_memory_ignores_dask(self) -> None:
        tree = make_synthetic_datatree(n_azimuth=20, n_range=30)
        chunked = tree.chunk()
        assert tree_memory_bytes(chunked) < tree_memory_bytes(tree)

    @pytest.mark.asyncio
    async def test_over_budget_evicts_then_warns_but_still_opens(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("XRADAR_MAX_OPEN_MB", "0.001")  # ~1 KB: any file exceeds it
        f = tmp_path / "big_volume.nc"
        f.write_bytes(b"x")

        def fake_open(self, path):
            self._dtree = make_synthetic_datatree(n_azimuth=20, n_range=30)
            self._format = "synthetic"
            return {"variables": ["DBZH"], "sweeps": [], "dimensions": {}, "attributes": {}}

        handler, _ = _make_handler()
        with (
            patch.object(RadarReader, "open_file", fake_open),
            patch.object(server._shared_renderer, "release_memory") as release,
        ):
            await handler.handle_message(json.dumps({"type": "open_file", "path": str(f)}))
        assert release.called  # caches were evicted first
        warnings = [m for m in handler._sent if m["type"] == "warning"]
        assert len(warnings) == 1
        assert "big_volume.nc" in warnings[0]["message"]
        assert warnings[0]["code"] == "memory_budget"
        assert warnings[0]["open_mb"] >= 0 and warnings[0]["max_mb"] == 0
        opened = [m for m in handler._sent if m["type"] == "file_opened"]
        assert len(opened) == 1  # the file still opens: never a silent crash or refusal
        types = [m["type"] for m in handler._sent]
        assert types.index("warning") < types.index("file_opened")

    @pytest.mark.asyncio
    async def test_within_budget_no_warning(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("XRADAR_MAX_OPEN_MB", "100000")
        f = tmp_path / "small.nc"
        f.write_bytes(b"x")

        def fake_open(self, path):
            self._dtree = make_synthetic_datatree(n_azimuth=20, n_range=30)
            self._format = "synthetic"
            return {"variables": ["DBZH"], "sweeps": [], "dimensions": {}, "attributes": {}}

        handler, _ = _make_handler()
        with patch.object(RadarReader, "open_file", fake_open):
            await handler.handle_message(json.dumps({"type": "open_file", "path": str(f)}))
        assert not any(m["type"] == "warning" for m in handler._sent)
        assert any(m["type"] == "file_opened" for m in handler._sent)

    @pytest.mark.asyncio
    async def test_ping_reports_memory(self, monkeypatch) -> None:
        monkeypatch.setenv("XRADAR_MAX_OPEN_MB", "512")
        server._register_reader(_reader_with_tree(make_synthetic_datatree()))
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "ping"}))
        pong = next(m for m in handler._sent if m["type"] == "pong")
        assert pong["status"] == "ok"  # existing fields untouched
        assert pong["memory"]["max_mb"] == 512
        assert pong["memory"]["files"] == 1
        assert pong["memory"]["open_mb"] > 0

    def test_release_caches_drops_arco_sweeps(self) -> None:
        import numpy as np
        import xarray as xr

        reader = RadarReader()
        reader._arco_loaded_sweeps = {0: xr.Dataset({"DBZH": (("a", "r"), np.zeros((10, 10)))})}
        assert reader.release_caches() == 800
        assert reader._arco_loaded_sweeps == {}

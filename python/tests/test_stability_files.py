"""Opening damaged, odd and hostile files: always a clear answer, never a crash or a leftover."""

from __future__ import annotations

import gzip
import json
import os
import random
import sys
import tempfile
from pathlib import Path

import pytest

import server
from engine.reader import RadarReader
from tests import radar_data
from tests.test_reliability_open import KTLX
from tests.test_server import _make_handler
from tests.web_helpers import make_netcdf

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


@pytest.fixture(autouse=True)
def _clean_server_state():
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


@pytest.fixture()
def private_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the system temp folder at an empty one so leftovers are exact, not guessed."""
    folder = tmp_path / "systmp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


@pytest.fixture()
def good_nc(tmp_path: Path) -> Path:
    return make_netcdf(tmp_path / "good.nc", n_azimuth=24, n_range=40)


async def _open(path: str) -> tuple[list[dict], list[dict]]:
    handler, _ = _make_handler()
    await handler.handle_message(json.dumps({"type": "open_file", "path": path}))
    sent = handler._sent
    return (
        [m for m in sent if m["type"] == "file_opened"],
        [m for m in sent if m["type"] == "error"],
    )


def _assert_clean_failure(errors: list[dict]) -> None:
    assert len(errors) == 1
    text = errors[0]["message"]
    assert text.startswith("open_file failed: ")  # App.svelte attributes the error by this prefix
    for leak in ("Traceback", "Unexpected error", "NoneType", "object has no attribute"):
        assert leak not in text, text


class TestDamagedFiles:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("fraction", [0.0, 0.001, 0.05, 0.3, 0.5, 0.9, 0.999])
    async def test_truncated_netcdf(
        self, good_nc: Path, tmp_path: Path, private_tmp: Path, fraction: float
    ) -> None:
        raw = good_nc.read_bytes()
        cut = tmp_path / "cut.nc"
        cut.write_bytes(raw[: max(1, int(len(raw) * fraction))])
        opened, errors = await _open(str(cut))
        if not opened:
            _assert_clean_failure(errors)
        assert list(private_tmp.iterdir()) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("seed", range(12))
    async def test_random_bit_flips_in_netcdf(
        self, good_nc: Path, tmp_path: Path, private_tmp: Path, seed: int
    ) -> None:
        rnd = random.Random(seed)
        raw = bytearray(good_nc.read_bytes())
        for _ in range(rnd.choice([1, 16, 256])):
            raw[rnd.randrange(len(raw))] ^= 1 << rnd.randrange(8)
        bad = tmp_path / "flipped.nc"
        bad.write_bytes(bytes(raw))
        opened, errors = await _open(str(bad))
        if opened:
            return  # flipped bits in data values are legal
        _assert_clean_failure(errors)
        assert list(private_tmp.iterdir()) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("seed", range(6))
    async def test_corrupt_gzip_never_leaves_the_decompressed_copy(
        self, good_nc: Path, tmp_path: Path, private_tmp: Path, seed: int
    ) -> None:
        rnd = random.Random(seed)
        packed = bytearray(gzip.compress(good_nc.read_bytes()))
        if seed % 2:
            packed = packed[: rnd.randrange(20, len(packed))]
        else:
            for _ in range(8):
                packed[rnd.randrange(10, len(packed))] ^= 0xFF
        bad = tmp_path / "bad.nc.gz"
        bad.write_bytes(bytes(packed))
        opened, errors = await _open(str(bad))
        if not opened:
            _assert_clean_failure(errors)
        # a copy that was opened successfully is already unlinked too (data stays readable)
        assert list(private_tmp.iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_good_gzip_opens_and_its_copy_goes_with_the_file(
        self, good_nc: Path, tmp_path: Path, private_tmp: Path
    ) -> None:
        packed = tmp_path / "good.nc.gz"
        packed.write_bytes(gzip.compress(good_nc.read_bytes()))
        opened, errors = await _open(str(packed))
        assert opened and not errors
        # the NetCDF tree reads from the decompressed copy, so it lives as long as the open file
        assert len(list(private_tmp.iterdir())) == 1
        import asyncio
        import gc

        await asyncio.gather(*list(server._background_tasks))  # the background prefetch
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "close_file", "file_id": opened[0]["file_id"]})
        )

        gc.collect()
        assert list(private_tmp.iterdir()) == []

    @pytest.mark.asyncio
    async def test_the_copy_goes_at_close_even_while_something_still_holds_the_reader(
        self, good_nc: Path, tmp_path: Path, private_tmp: Path
    ) -> None:
        """Closing must not wait for the garbage collector: a late prefetch, a cache entry or a
        traceback can keep the reader alive for a long time (this failed on macOS CI)."""
        packed = tmp_path / "good.nc.gz"
        packed.write_bytes(gzip.compress(good_nc.read_bytes()))
        opened, errors = await _open(str(packed))
        assert opened and not errors
        held = server._readers()[opened[0]["file_id"]]  # still referenced after close_file
        assert len(list(private_tmp.iterdir())) == 1
        handler, _ = _make_handler()
        await handler.handle_message(
            json.dumps({"type": "close_file", "file_id": opened[0]["file_id"]})
        )
        assert list(private_tmp.iterdir()) == []
        assert held is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            b"\x1f\x8b\x08\x00\x00\x00\x00\x00",  # gzip header, nothing else
            b"\x1f\x8b" + bytes(range(200)),  # gzip magic, garbage after it
            gzip.compress(b""),  # empty archive
            gzip.compress(b"just text\n" * 50),  # archive of a text file
            gzip.compress(gzip.compress(os.urandom(5000))),  # archive of an archive
            b"BZh9" + bytes(range(256)),  # bzip2 magic only
            b"\x89HDF\r\n\x1a\n" + b"\x00" * 100,  # HDF5 signature only
            b"CDF\x01" + b"\x00" * 100,  # NetCDF classic signature only
            b"AR2V0006" + b"\x00" * 200,  # NEXRAD signature only
            b"\x00" * 50_000,
        ],
    )
    async def test_hostile_payloads(
        self, tmp_path: Path, private_tmp: Path, payload: bytes
    ) -> None:
        for name in ("p.nc", "p.gz", "p", "p.h5", "p.RAW"):
            f = tmp_path / name
            f.write_bytes(payload)
            opened, errors = await _open(str(f))
            assert not opened
            _assert_clean_failure(errors)
        assert list(private_tmp.iterdir()) == []

    @pytest.mark.asyncio
    async def test_wrong_extension_still_opens_by_content(
        self, good_nc: Path, tmp_path: Path
    ) -> None:
        for name in ("scan.png", "scan.txt", "scan", "scan.RAW", "scan.h5"):
            f = tmp_path / name
            f.write_bytes(good_nc.read_bytes())
            opened, errors = await _open(str(f))
            assert opened or errors  # never silence
            if errors:
                _assert_clean_failure(errors)

    @pytest.mark.skipif(not KTLX.exists(), reason="KTLX sample not available")
    @pytest.mark.asyncio
    @pytest.mark.parametrize("seed", range(4))
    async def test_corrupt_real_nexrad(self, tmp_path: Path, private_tmp: Path, seed: int) -> None:
        rnd = random.Random(seed)
        raw = bytearray(gzip.decompress(KTLX.read_bytes()))
        for _ in range(32):
            raw[rnd.randrange(24, len(raw))] ^= 1 << rnd.randrange(8)
        if seed % 2:
            raw[rnd.randrange(24, 2000) :][:512] = os.urandom(512)
        bad = tmp_path / "KTLX_flipped_V06"
        bad.write_bytes(bytes(raw))
        opened, errors = await _open(str(bad))
        if not opened:
            _assert_clean_failure(errors)
        assert list(private_tmp.iterdir()) == []


class TestGzippedLazyFormats:
    """A gzipped NetCDF/HDF5 file is read from its decompressed copy on demand."""

    def test_data_stays_readable_after_open(self, good_nc: Path, tmp_path: Path, private_tmp: Path):
        import gc
        import threading

        packed = tmp_path / "good.nc.gz"
        packed.write_bytes(gzip.compress(good_nc.read_bytes()))
        reader = RadarReader()
        reader.open_file(str(packed))
        assert len(list(private_tmp.iterdir())) == 1  # kept: the tree reads from it

        errors: list[str] = []

        def work() -> None:
            try:
                for sweep in range(2):
                    for var in ("DBZH", "VRADH"):
                        assert reader.get_sweep_data(sweep, var).values.shape == (24, 40)
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        gc.collect()
        work()
        assert errors == []

    def test_copy_is_removed_on_close_replace_and_garbage_collection(
        self, good_nc: Path, tmp_path: Path, private_tmp: Path
    ) -> None:
        import gc

        packed = tmp_path / "good.nc.gz"
        packed.write_bytes(gzip.compress(good_nc.read_bytes()))
        reader = RadarReader()
        reader.open_file(str(packed))
        reader.close()
        assert list(private_tmp.iterdir()) == []

        reader.open_file(str(packed))
        reader.open_file(str(good_nc))  # a different file replaces it
        assert list(private_tmp.iterdir()) == []

        other = RadarReader()
        other.open_file(str(packed))
        assert len(list(private_tmp.iterdir())) == 1
        del other
        gc.collect()
        assert list(private_tmp.iterdir()) == []

    def test_a_failed_second_open_keeps_the_first_files_copy(
        self, good_nc: Path, tmp_path: Path, private_tmp: Path
    ) -> None:
        packed = tmp_path / "good.nc.gz"
        packed.write_bytes(gzip.compress(good_nc.read_bytes()))
        reader = RadarReader()
        reader.open_file(str(packed))
        bad = tmp_path / "bad.nc.gz"
        bad.write_bytes(gzip.compress(b"not radar data at all" * 20))
        with pytest.raises(ValueError):
            reader.open_file(str(bad))
        assert reader.get_sweep_data(0, "DBZH").values.shape == (24, 40)
        assert len(list(private_tmp.iterdir())) == 1


class TestOddPaths:
    @pytest.mark.asyncio
    async def test_folder_is_not_a_file(self, tmp_path: Path) -> None:
        (tmp_path / "scans.nc").mkdir()
        opened, errors = await _open(str(tmp_path / "scans.nc"))
        assert not opened
        _assert_clean_failure(errors)
        assert "folder" in errors[0]["message"]
        assert "inside" in errors[0]["message"]  # says what to do

    @pytest.mark.asyncio
    async def test_unicode_and_spaces(self, good_nc: Path, tmp_path: Path) -> None:
        for name in ("scan with spaces.nc", "été 中文 radar.nc", "a'b\"c;d&e.nc"):
            dst = tmp_path / name
            dst.write_bytes(good_nc.read_bytes())
            opened, errors = await _open(str(dst))
            assert opened and not errors, (name, errors)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "needle"),
        [
            ("relative/scan.nc", "absolute"),
            ("", "missing 'path'"),
            ("/nonexistent-folder/a" + "b" * 300 + ".nc", "not found"),
        ],
    )
    async def test_unusable_paths(self, path: str, needle: str) -> None:
        opened, errors = await _open(path)
        assert not opened and len(errors) == 1
        assert needle in errors[0]["message"]

    @pytest.mark.asyncio
    async def test_nul_byte_in_path(self) -> None:
        opened, errors = await _open("/tmp/a\x00b.nc")
        assert not opened and len(errors) == 1
        assert "Traceback" not in errors[0]["message"]

    @pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks and FIFOs")
    @pytest.mark.asyncio
    async def test_symlink_loop_dangling_link_and_fifo(self, tmp_path: Path) -> None:
        os.symlink(tmp_path / "b", tmp_path / "a")
        os.symlink(tmp_path / "a", tmp_path / "b")
        os.symlink(tmp_path / "missing", tmp_path / "dangling.nc")
        os.mkfifo(tmp_path / "pipe.nc")  # opening it for reading would block forever
        for name in ("a", "dangling.nc", "pipe.nc"):
            opened, errors = await asyncio_wait(_open(str(tmp_path / name)))
            assert not opened
            _assert_clean_failure(errors)

    @pytest.mark.asyncio
    async def test_failed_open_keeps_the_previous_file(self, good_nc: Path, tmp_path: Path) -> None:
        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "open_file", "path": str(good_nc)}))
        first = next(m for m in handler._sent if m["type"] == "file_opened")
        bad = tmp_path / "bad.nc"
        bad.write_bytes(b"not radar")
        await handler.handle_message(json.dumps({"type": "open_file", "path": str(bad)}))
        assert any(m["type"] == "error" for m in handler._sent)
        await handler.handle_message(json.dumps({"type": "ping"}))
        pong = handler._sent[-1]
        assert pong["type"] == "pong" and pong["file_open"] is True
        assert list(server._file_readers) == [first["file_id"]]


async def asyncio_wait(coro, timeout: float = 20):
    import asyncio

    return await asyncio.wait_for(coro, timeout)


class TestConcurrentOpens:
    @pytest.mark.asyncio
    async def test_two_files_opening_at_once_both_end_up_open(
        self, good_nc: Path, tmp_path: Path
    ) -> None:
        import asyncio

        other = make_netcdf(tmp_path / "other.nc", n_azimuth=36, n_range=50)
        handler, _ = _make_handler()
        await asyncio.gather(
            handler.handle_message(json.dumps({"type": "open_file", "path": str(good_nc)})),
            handler.handle_message(json.dumps({"type": "open_file", "path": str(other)})),
            handler.handle_message(json.dumps({"type": "open_file", "path": str(tmp_path)})),
        )
        opened = [m for m in handler._sent if m["type"] == "file_opened"]
        assert len(opened) == 2 and len({m["file_id"] for m in opened}) == 2
        assert len([m for m in handler._sent if m["type"] == "error"]) == 1
        shapes = set()
        for m in opened:
            await handler.handle_message(
                json.dumps(
                    {
                        "type": "get_sweep_data",
                        "variable": "DBZH",
                        "sweep": 0,
                        "file_id": m["file_id"],
                    }
                )
            )
            shapes.add(
                next(h for h in reversed(handler._sent) if h["type"] == "sweep_data")["n_az"]
            )
        assert shapes == {24, 36}  # each file answers with its own data

    @pytest.mark.asyncio
    async def test_closing_a_file_that_is_being_read(self, good_nc: Path) -> None:
        import asyncio

        handler, _ = _make_handler()
        await handler.handle_message(json.dumps({"type": "open_file", "path": str(good_nc)}))
        fid = next(m for m in handler._sent if m["type"] == "file_opened")["file_id"]
        get = {"type": "get_sweep_data", "variable": "DBZH", "sweep": 0, "file_id": fid}
        await asyncio.gather(
            handler.handle_message(json.dumps(get)),
            handler.handle_message(json.dumps({"type": "close_file", "file_id": fid})),
            handler.handle_message(json.dumps(get)),
        )
        # every request got exactly one answer (data or a clear error), none was lost
        replies = [m for m in handler._sent if m["type"] in ("sweep_data", "error", "file_closed")]
        assert len(replies) == 3
        for m in handler._sent:
            if m["type"] == "error":
                assert "Traceback" not in m["message"]
        assert fid not in server._file_readers


def test_odim_and_cfradial_samples_if_present(tmp_path: Path) -> None:
    """Real ODIM/CfRadial files from open-radar-data: truncate and make sure it is a clean error."""
    sample = radar_data.get(radar_data.DOW8_RHI_2)
    if sample is None:
        pytest.skip("open-radar-data sample not available")
    raw = sample.read_bytes()
    for fraction in (0.01, 0.5, 0.95):
        cut = tmp_path / f"cut{fraction}.nc"
        cut.write_bytes(raw[: int(len(raw) * fraction)])
        try:
            RadarReader().open_file(str(cut))
        except ValueError as exc:
            assert "Could not open" in str(exc)

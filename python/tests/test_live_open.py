"""Tests for the live map's held files, pinning (opening a held volume as a normal file), the
display floor / smoothing of the picture, the credit in exports and the grid choice.

No network: the volumes are synthetic readers and small files.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import engine.live as live
from engine import mosaic
from tests.test_live import FakeReader, _handler, _vol, needs_radarx
from tests.test_mosaic import flat, make_reader, make_volume


@pytest.fixture()
def pins() -> live.PinRegistry:
    reg = live.PinRegistry(max_pinned=2)
    yield reg
    reg.clear()


def _held_with_file(
    tmp_path: Path, st: str, name: str, payload: bytes = b"x" * 64
) -> live.HeldVolume:
    f = tmp_path / name
    f.write_bytes(payload)
    v = live.HeldVolume(
        st, name, f"s3://b/{name}", len(payload), "2026-01-01T00:00:00Z", FakeReader(name)
    )
    v.local_path = str(f)
    return v


class TestHeldFiles:
    def test_the_kept_file_goes_with_the_volume(self, tmp_path: Path) -> None:
        c = live.LiveCache()
        c.hold(_held_with_file(tmp_path, "KTLX", "a"))
        assert (tmp_path / "a").is_file()
        c.hold(_held_with_file(tmp_path, "KTLX", "b"))  # the newer volume of the same station
        assert not (tmp_path / "a").exists() and (tmp_path / "b").is_file()
        c.clear()
        assert not (tmp_path / "b").exists()

    def test_status_says_whether_the_file_is_still_on_disk(self, tmp_path: Path) -> None:
        c = live.LiveCache()
        c.hold(_held_with_file(tmp_path, "KTLX", "a"))
        assert c.status()["stations"][0]["on_disk"] is True


class TestPinning:
    def test_a_pinned_volume_survives_the_next_update_and_eviction(
        self, tmp_path: Path, pins: live.PinRegistry
    ) -> None:
        c = live.LiveCache(max_stations=1)
        c.hold(_held_with_file(tmp_path, "KTLX", "old", b"a" * 100))
        (pin,) = live.pin_volumes(["ktlx"], cache=c, pins=pins)
        assert (pin.station, pin.name, pin.source, pin.size) == ("KTLX", "old", "s3://b/old", 100)
        assert Path(pin.path).read_bytes() == b"a" * 100
        # the live cache moves on: a newer volume replaces it, another station evicts that one
        c.hold(_held_with_file(tmp_path, "KTLX", "new", b"b" * 50))
        c.hold(_held_with_file(tmp_path, "KVNX", "other", b"c" * 10))  # max_stations=1
        assert not (tmp_path / "old").exists()
        assert Path(pin.path).read_bytes() == b"a" * 100  # still there, still the old volume
        assert pins.status()["count"] == 1 and pins.status()["bytes"] == 100

    def test_releasing_deletes_the_file_and_frees_the_slot(
        self, tmp_path: Path, pins: live.PinRegistry
    ) -> None:
        c = live.LiveCache()
        c.hold(_held_with_file(tmp_path, "KTLX", "a"))
        (pin,) = live.pin_volumes(["KTLX"], cache=c, pins=pins)
        reader = FakeReader("x")
        reader._live_pin = pin.key
        assert live.release_reader(reader, pins=pins) is True
        assert not Path(pin.path).exists() and len(pins) == 0 and reader.closed
        assert live.release_reader(FakeReader("plain"), pins=pins) is False  # not a live volume

    def test_the_number_of_open_volumes_is_bounded(
        self, tmp_path: Path, pins: live.PinRegistry
    ) -> None:
        c = live.LiveCache()
        for st in ("KAAA", "KBBB", "KCCC"):
            c.hold(_held_with_file(tmp_path, st, f"{st}_v"))
        live.pin_volumes(["KAAA", "KBBB"], cache=c, pins=pins)
        with pytest.raises(live.LiveError, match="At most 2"):
            live.pin_volumes(["KCCC"], cache=c, pins=pins)
        assert len(pins) == 2

    def test_a_failure_rolls_back_the_pins_of_that_call(
        self, tmp_path: Path, pins: live.PinRegistry
    ) -> None:
        c = live.LiveCache()
        c.hold(_held_with_file(tmp_path, "KAAA", "a"))
        with pytest.raises(live.LiveError, match="holds no volume"):
            live.pin_volumes(["KAAA", "KBBB"], cache=c, pins=pins)
        assert len(pins) == 0

    def test_a_volume_already_open_is_not_pinned_twice(
        self, tmp_path: Path, pins: live.PinRegistry
    ) -> None:
        c = live.LiveCache()
        c.hold(_held_with_file(tmp_path, "KAAA", "a"))
        (p1,) = live.pin_volumes(["KAAA"], cache=c, pins=pins)
        (p2,) = live.pin_volumes(["KAAA"], cache=c, pins=pins)
        assert p1 is p2 and len(pins) == 1

    def test_no_file_on_disk_says_what_to_do(self, pins: live.PinRegistry) -> None:
        c = live.LiveCache()
        c.hold(_vol("KAAA"))  # a volume without a kept file
        with pytest.raises(live.LiveError, match="Update now"):
            live.pin_volumes(["KAAA"], cache=c, pins=pins)

    def test_falls_back_to_a_copy_when_a_hard_link_is_not_possible(
        self, tmp_path: Path, pins: live.PinRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_link(*_a: object, **_k: object) -> None:
            raise OSError("cross-device")

        monkeypatch.setattr(live.os, "link", no_link)
        c = live.LiveCache()
        c.hold(_held_with_file(tmp_path, "KAAA", "a", b"z" * 33))
        (pin,) = live.pin_volumes(["KAAA"], cache=c, pins=pins)
        assert pin.copied is True and Path(pin.path).read_bytes() == b"z" * 33

    def test_pinning_makes_no_request(
        self, tmp_path: Path, pins: live.PinRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import engine.cloud as cloud

        def boom() -> None:
            raise AssertionError("pinning must not touch the network")

        monkeypatch.setattr(cloud, "_s3", boom)
        c = live.LiveCache()
        c.hold(_held_with_file(tmp_path, "KTLX", "a"))
        live.pin_volumes(["KTLX"], cache=c, pins=pins)


def _opener(value: float):
    def opener(path: str):
        reader = make_reader(
            make_volume(35.3, -97.3, flat(value), name=path.rsplit("/", 1)[-1], n_range=60)
        )
        reader._path = path
        return reader

    return opener


def _record(value: float):
    c = live.LiveCache()
    files = [{"station": "KTLX", "path": "s3://b/a", "name": "a", "size": 5}]
    rec, radars = live.update(files, cache=c, opener=_opener(value), grid={"resolution_m": 2000.0})
    return rec, radars, c


@needs_radarx
class TestPicture:
    def test_floor_hides_weak_echo_in_the_picture_only(self) -> None:
        rec, _r, _c = _record(2.0)  # weak echo everywhere it is measured
        before = rec.image.copy()
        png, info = live.render_display_png(rec, 5.0)
        alpha = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[..., 3]
        assert alpha.max() == 0  # nothing at 2 dBZ is drawn with a 5 dBZ floor
        assert np.isfinite(before).any()
        np.testing.assert_array_equal(
            np.nan_to_num(rec.image, nan=-999), np.nan_to_num(before, nan=-999)
        )
        assert info["floor_dbz"] == 5.0 and "not drawn" in info["note"]
        png_all, info_all = live.render_display_png(rec, None)
        alpha_all = np.asarray(Image.open(io.BytesIO(png_all)).convert("RGBA"))[..., 3]
        assert alpha_all.max() == 255 and info_all["floor_dbz"] is None

    def test_strong_echo_is_drawn_and_the_picture_is_smoothed(self) -> None:
        rec, _r, _c = _record(35.0)
        png, info = live.render_display_png(rec, 5.0)
        w, h = Image.open(io.BytesIO(png)).size
        ny, nx = rec.image.shape
        assert info["smoothing"] == 3 and (w, h) == (nx * 3, ny * 3)
        assert np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[..., 3].max() == 255

    def test_update_gives_the_frame_its_picture_credit_and_grid(self) -> None:
        rec, radars, c = _record(30.0)
        assert rec.display["floor_dbz"] == 5.0
        assert "NOAA" in rec.extra_attrs["data_credit"]
        assert "not an official" in rec.extra_attrs["live_composite"]
        header = live.frame_header(rec, radars, cache=c)
        assert header["display"]["floor_dbz"] == 5.0 and header["credit"]["licence"]
        assert header["grid"]["resolution_m"] == 2000.0

    def test_default_grid_follows_the_number_of_radars(self) -> None:
        c = live.LiveCache()
        files = [{"station": "KTLX", "path": "s3://b/a", "name": "a", "size": 5}]
        rec, _radars = live.update(files, cache=c, opener=_opener(30.0))
        assert rec.resolution_m == 1000.0  # one radar: the finest grid

    def test_restyle_redraws_a_cached_composite(self) -> None:
        rec, _r, _c = _record(2.0)
        again = live.restyle(rec.mosaic_id, None)
        assert again.display["floor_dbz"] is None
        with pytest.raises(mosaic.MosaicError):
            live.restyle("nope", 5.0)

    def test_exports_carry_the_credit_and_the_unfloored_values(self, tmp_path: Path) -> None:
        import xarray as xr

        rec, _r, _c = _record(2.0)
        nc = tmp_path / "live.nc"
        mosaic.write_netcdf(rec, nc)
        with xr.open_dataset(nc) as ds:
            assert "NOAA" in ds.attrs["data_credit"]
            assert "public domain" in ds.attrs["data_licence"]
            assert "unfloored" in ds.attrs["display_note"]
            fields = [v for v in ds.data_vars if ds[v].ndim == 2 and v not in ("lat", "lon")]
            assert any(np.isfinite(ds[v].values).any() for v in fields)  # the 2 dBZ echo is kept
        assert b"NOAA" in mosaic.png_bytes(rec)  # in the PNG text chunks

    def test_clamp_floor(self) -> None:
        assert live.clamp_floor(None) is None
        assert live.clamp_floor(5) == 5.0
        assert live.clamp_floor(-99) == live.MIN_FLOOR_DBZ
        assert live.clamp_floor(99) == live.MAX_FLOOR_DBZ
        for bad in ("5", True, float("nan")):
            with pytest.raises(live.LiveError):
                live.clamp_floor(bad)


class TestGrid:
    def test_finer_for_few_radars(self) -> None:
        assert live.requested_resolution_m(1) == 1000.0
        assert live.requested_resolution_m(4) == 1000.0
        assert live.requested_resolution_m(5) == 1500.0
        assert live.requested_resolution_m(8) == 2000.0

    def test_a_wide_spread_gets_a_coarser_grid_not_an_error(self) -> None:
        close = [(35.3, -97.3), (36.1, -97.0)]
        far = [(25.0, -80.0), (47.0, -122.0)]
        assert live.fit_resolution_m(close, 1000.0, 230.0) == 1000.0
        assert 1000.0 < live.fit_resolution_m(far, 1000.0, 230.0) <= 5000.0
        assert live.fit_resolution_m([], 1000.0, 230.0) == 1000.0


class TestStaleFolders:
    @pytest.mark.skipif(live.os.name != "posix", reason="needs a pid probe")
    def test_folders_of_dead_engines_are_removed_and_live_ones_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(live.tempfile, "gettempdir", lambda: str(tmp_path))
        dead = tmp_path / "radarx-live-2147480000-abc"
        mine = tmp_path / f"radarx-live-{live.os.getpid()}-xyz"
        other = tmp_path / "unrelated-folder"
        for d in (dead, mine, other):
            d.mkdir()
            (d / "f").write_bytes(b"1")
        live._sweep_stale_roots()
        assert not dead.exists() and mine.exists() and other.exists()


class TestServerOpen:
    def test_open_volumes_registers_files_and_close_releases_the_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import server

        reg = live.PinRegistry()
        monkeypatch.setattr(live, "PINS", reg)
        live.LIVE.clear()
        live.LIVE.hold(_held_with_file(tmp_path, "KTLX", "a", b"q" * 20))

        def fake_open(pin: live.PinnedVolume):
            reader = FakeReader(pin.path)
            reader._live_pin = pin.key
            return reader, {"variables": [], "sweeps": [], "source": pin.source}

        monkeypatch.setattr(live, "open_pinned", fake_open)
        monkeypatch.setattr(server.layer_provenance, "describe_reader", lambda *_a, **_k: {})
        h, sent = _handler()
        try:
            asyncio.run(
                h.handle_message(
                    json.dumps({"type": "live_open_volumes", "request_id": 7, "stations": ["KTLX"]})
                )
            )
            types = [m["type"] for m in sent if isinstance(m, dict)]
            assert "file_opened" in types
            done = next(
                m for m in sent if isinstance(m, dict) and m["type"] == "live_volumes_opened"
            )
            assert done["request_id"] == 7 and done["files"][0]["station"] == "KTLX"
            assert done["pinned"]["count"] == 1 and done["pinned"]["bytes"] == 20
            file_id = done["files"][0]["file_id"]
            # the live map stops and clears its cache: the opened file is unaffected
            asyncio.run(h.handle_message(json.dumps({"type": "live_stop", "request_id": 8})))
            assert len(reg) == 1
            asyncio.run(h.handle_message(json.dumps({"type": "close_file", "file_id": file_id})))
            assert len(reg) == 0
        finally:
            server._readers().clear()
            live.LIVE.clear()

    def test_a_volume_open_in_the_engine_but_unknown_to_the_page_is_announced_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import server

        reg = live.PinRegistry()
        monkeypatch.setattr(live, "PINS", reg)
        live.LIVE.clear()
        live.LIVE.hold(_held_with_file(tmp_path, "KTLX", "a", b"q" * 20))

        def fake_open(pin: live.PinnedVolume):
            reader = FakeReader(pin.path)
            reader._live_pin = pin.key
            reader.get_schema = lambda: {"variables": [], "sweeps": []}
            return reader, {"variables": [], "sweeps": [], "source": pin.source}

        monkeypatch.setattr(live, "open_pinned", fake_open)
        monkeypatch.setattr(server.layer_provenance, "describe_reader", lambda *_a, **_k: {})
        h, sent = _handler()
        req = {"type": "live_open_volumes", "request_id": 1, "stations": ["KTLX"]}
        try:
            asyncio.run(h.handle_message(json.dumps(req)))
            first = next(m for m in sent if isinstance(m, dict) and m["type"] == "file_opened")
            fid = first["file_id"]
            sent.clear()
            # the page knows the file: nothing is announced again
            asyncio.run(h.handle_message(json.dumps({**req, "known_file_ids": [fid]})))
            assert not [m for m in sent if isinstance(m, dict) and m["type"] == "file_opened"]
            assert sent[-1]["files"][0]["already_open"] is True
            sent.clear()
            # the page was reloaded and does not: it is announced with the same id
            asyncio.run(h.handle_message(json.dumps({**req, "known_file_ids": []})))
            again = [m for m in sent if isinstance(m, dict) and m["type"] == "file_opened"]
            assert len(again) == 1 and again[0]["file_id"] == fid
            assert again[0]["data"]["source"] == "s3://b/a"
            assert len(reg) == 1  # still one pinned file
        finally:
            server._readers().clear()
            live.LIVE.clear()
            reg.clear()

    def test_open_volumes_without_a_held_volume_is_a_plain_error(self) -> None:
        live.LIVE.clear()
        h, sent = _handler()
        asyncio.run(
            h.handle_message(
                json.dumps({"type": "live_open_volumes", "request_id": 1, "stations": ["KTLX"]})
            )
        )
        assert sent[-1]["type"] == "live_error" and "holds no volume" in sent[-1]["message"]

    def test_restyle_unknown_composite_is_a_live_error(self) -> None:
        h, sent = _handler()
        asyncio.run(
            h.handle_message(
                json.dumps(
                    {"type": "live_restyle", "request_id": 2, "mosaic_id": "gone", "floor_dbz": 5}
                )
            )
        )
        assert sent[-1]["type"] == "live_error"

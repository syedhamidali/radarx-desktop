"""Tests for engine.live (the live map) and its server messages: no network, a fake bucket."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import engine.cloud as cloud
import engine.live as live
from engine import mosaic
from tests.test_cloud import FakeS3
from tests.test_mosaic import flat, make_reader, make_volume

needs_radarx = pytest.mark.skipif(not mosaic.radarx_available(), reason="radarx is not installed")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y/%m/%d")


def _name(st: str, minutes_ago: int) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return f"{st}{t:%Y%m%d_%H%M%S}_V06"


@pytest.fixture()
def bucket(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    """A fake public bucket with today's newest volumes of KTLX and KVNX; KINX has none."""
    day = _today()
    client = FakeS3(
        [
            f"{day}/KTLX/{_name('KTLX', 9)}",
            f"{day}/KTLX/{_name('KTLX', 4)}",
            f"{day}/KVNX/{_name('KVNX', 6)}",
        ]
    )
    monkeypatch.setattr(cloud, "_s3", lambda: client)
    cloud._DAY_CACHE.clear()
    cloud._INFO_CACHE.clear()
    return client


class FakeReader:
    """Stands in for RadarReader: remembers whether it was closed."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _vol(st: str, name: str = "x", size: int = 10, t: float | None = None) -> live.HeldVolume:
    v = live.HeldVolume(st, name, f"s3://b/{name}", size, None, FakeReader(name))
    if t is not None:
        v.opened_at = t
    return v


class TestLiveCache:
    def test_newest_volume_per_station_replaces_and_closes_the_old_one(self) -> None:
        c = live.LiveCache()
        old = _vol("KTLX", "old")
        c.hold(old)
        new = _vol("KTLX", "new")
        c.hold(new)
        assert len(c) == 1
        assert c.holds("KTLX", "new") and not c.holds("KTLX", "old")
        assert old.reader.closed and not new.reader.closed

    def test_station_count_bound_evicts_the_oldest_other_station(self) -> None:
        c = live.LiveCache(max_stations=2)
        c.hold(_vol("KAAA", t=1))
        c.hold(_vol("KBBB", t=2))
        evicted = c.hold(_vol("KCCC", t=3))
        assert evicted == ["KAAA"]
        assert c.stations() == ["KBBB", "KCCC"]

    def test_byte_cap_never_evicts_the_volume_just_held(self) -> None:
        c = live.LiveCache(max_bytes=25)
        c.hold(_vol("KAAA", size=10, t=1))
        c.hold(_vol("KBBB", size=10, t=2))
        evicted = c.hold(_vol("KCCC", size=30, t=3))  # alone over the cap: others go, it stays
        assert evicted == ["KAAA", "KBBB"]
        assert c.stations() == ["KCCC"]
        assert c.nbytes == 30

    def test_keep_only_and_clear_close_readers(self) -> None:
        c = live.LiveCache()
        a, b = _vol("KAAA"), _vol("KBBB")
        c.hold(a)
        c.hold(b)
        assert c.keep_only(["kbbb"]) == ["KAAA"]
        assert a.reader.closed and not b.reader.closed
        c.clear()
        assert b.reader.closed and len(c) == 0
        assert c.status()["stations"] == []


class TestNormalize:
    def test_upper_dedupe_order_and_limit(self) -> None:
        assert live.normalize_stations(["ktlx", "KVNX", "KTLX"]) == ["KTLX", "KVNX"]
        with pytest.raises(live.LiveError):
            live.normalize_stations([])
        with pytest.raises(live.LiveError):
            live.normalize_stations(["K" + str(i).zfill(3) for i in range(9)])
        with pytest.raises(ValueError):
            live.normalize_stations(["nope!"])


class TestCheck:
    def test_lists_newest_volume_and_reports_held_and_missing(self, bucket: FakeS3) -> None:
        c = live.LiveCache()
        res = live.check(["KTLX", "KVNX", "KINX"], cache=c)
        by = {r["station"]: r for r in res}
        assert by["KTLX"]["status"] == "ok" and by["KTLX"]["name"] == _name("KTLX", 4)
        assert by["KTLX"]["held"] is False and 200 < by["KTLX"]["age_s"] < 400
        assert by["KVNX"]["status"] == "ok"
        assert by["KINX"]["status"] == "not_found" and "age_s" not in by["KINX"]
        c.hold(_vol("KTLX", _name("KTLX", 4)))
        cloud._INFO_CACHE.clear()
        assert live.check(["KTLX"], cache=c)[0]["held"] is True

    def test_offline_is_reported_not_raised(self, bucket: FakeS3) -> None:
        bucket.fail = True
        res = live.check(["KTLX"], cache=live.LiveCache())
        assert res[0]["status"] == "error"  # OSError('boom') is not a network class
        assert res[0]["held"] is False


def _synthetic_reader(path: str):
    """A small synthetic volume at a site near KTLX (no file, no network)."""
    reader = make_reader(
        make_volume(35.3, -97.3, flat(30.0), name=path.rsplit("/", 1)[-1], n_range=60)
    )
    reader._path = path
    return reader


@needs_radarx
class TestUpdate:
    def test_downloads_only_what_is_missing_and_builds_a_composite(self) -> None:
        c = live.LiveCache()
        opened: list[str] = []

        def opener(path: str):
            opened.append(path)
            return _synthetic_reader(path)

        files = [
            {"station": "KTLX", "path": "s3://b/a", "name": "a", "size": 5},
            {"station": "KVNX", "path": "s3://b/b", "name": "b", "size": 7},
        ]
        rec, radars = live.update(files, cache=c, opener=opener, grid={"resolution_m": 4000.0})
        assert opened == ["s3://b/a", "s3://b/b"]
        assert [r["downloaded"] for r in radars] == [True, True]
        assert rec.product == "composite" and len(rec.radars) == 2
        header = live.frame_header(rec, radars, cache=c)
        assert header["provenance"]["kind"] == "retrieved"
        assert header["cache"]["bytes"] == 12
        # second update with the same names: nothing is downloaded again
        _rec2, radars2 = live.update(files, cache=c, opener=opener, grid={"resolution_m": 4000.0})
        assert opened == ["s3://b/a", "s3://b/b"]
        assert [r["downloaded"] for r in radars2] == [False, False]

    def test_failed_download_is_reported_and_the_rest_is_merged(self) -> None:
        c = live.LiveCache()

        def opener(path: str):
            if path.endswith("/bad"):
                raise RuntimeError("Could not download")
            return _synthetic_reader(path)

        files = [
            {"station": "KTLX", "path": "s3://b/a", "name": "a", "size": 1},
            {"station": "KINX", "path": "s3://b/bad", "name": "bad", "size": 1},
        ]
        rec, radars = live.update(files, cache=c, opener=opener, grid={"resolution_m": 4000.0})
        assert radars[1]["status"] == "error" and "Could not download" in radars[1]["message"]
        assert len(rec.radars) == 1 and c.stations() == ["KTLX"]

    def test_nothing_opened_is_an_error(self) -> None:
        def opener(path: str):
            raise RuntimeError("offline")

        with pytest.raises(live.LiveError, match="No radar volume"):
            live.update(
                [{"station": "KTLX", "path": "s3://b/a", "name": "a"}],
                cache=live.LiveCache(),
                opener=opener,
            )

    def test_bad_requests(self) -> None:
        with pytest.raises(live.LiveError):
            live.update([], cache=live.LiveCache())
        with pytest.raises(live.LiveError, match="s3://"):
            live.update(
                [{"station": "KTLX", "path": "/etc/passwd", "name": "x"}], cache=live.LiveCache()
            )
        with pytest.raises(live.LiveError, match="twice"):
            live.update(
                [
                    {"station": "KTLX", "path": "s3://b/a", "name": "a"},
                    {"station": "ktlx", "path": "s3://b/b", "name": "b"},
                ],
                cache=live.LiveCache(),
            )


# ── Server messages ────────────────────────────────────────────────────────────────────────


def _handler():
    import server

    ws = AsyncMock()
    ws.remote_address = ("127.0.0.1", 1)
    sent: list = []

    async def _send(raw) -> None:
        sent.append(json.loads(raw) if isinstance(raw, str) else bytes(raw))

    ws.send = _send
    h = server.ConnectionHandler(ws)
    return h, sent


class TestServer:
    def test_live_check_routes_and_validates(self, bucket: FakeS3) -> None:
        h, sent = _handler()
        asyncio.run(
            h.handle_message(
                json.dumps({"type": "live_check", "request_id": 1, "stations": ["ktlx"]})
            )
        )
        assert sent[-1]["type"] == "live_check_result" and sent[-1]["request_id"] == 1
        assert sent[-1]["stations"][0]["station"] == "KTLX"
        asyncio.run(
            h.handle_message(json.dumps({"type": "live_check", "request_id": 2, "stations": []}))
        )
        assert sent[-1]["type"] == "live_error" and sent[-1]["request_id"] == 2

    def test_live_update_bad_request_is_a_live_error_and_closes_progress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mosaic, "_radarx", lambda: (None, "0"))
        h, sent = _handler()
        asyncio.run(
            h.handle_message(json.dumps({"type": "live_update", "request_id": 3, "files": []}))
        )
        types = [m["type"] for m in sent if isinstance(m, dict)]
        assert "live_error" in types
        assert sent[-1]["type"] == "progress" and sent[-1]["percent"] == 100

    def test_live_stop_clears_the_cache(self) -> None:
        live.LIVE.hold(_vol("KTLX"))
        h, sent = _handler()
        asyncio.run(h.handle_message(json.dumps({"type": "live_stop", "request_id": 4})))
        assert sent[-1] == {"type": "live_stopped", "request_id": 4}
        assert len(live.LIVE) == 0

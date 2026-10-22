"""Tests for engine.cloud (S3 listing caches, validation, ARCO helpers) — no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

import engine.cloud as cloud
from tests.conftest import ARCO_SCAN_KEY

_MODIFIED = datetime(2024, 1, 1, tzinfo=UTC)


class FakePaginator:
    def __init__(self, owner: FakeS3) -> None:
        self.owner = owner

    def paginate(self, **kwargs):
        self.owner.calls.append(kwargs)
        if self.owner.fail:
            raise OSError("boom")
        prefix = kwargs.get("Prefix", "")
        keys = [k for k in self.owner.keys if k.startswith(prefix)]
        # two pages to exercise pagination
        half = len(keys) // 2
        for chunk in (keys[:half], keys[half:]):
            yield {
                "Contents": [{"Key": k, "Size": 10, "LastModified": _MODIFIED} for k in chunk],
                "CommonPrefixes": [{"Prefix": "KLOT/"}, {"Prefix": "KATX/"}] if not prefix else [],
            }


class FakeS3:
    def __init__(self, keys: list[str] | None = None) -> None:
        self.keys = keys or []
        self.calls: list[dict] = []
        self.fail = False

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_objects_v2"
        return FakePaginator(self)


@pytest.fixture()
def fake_s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    client = FakeS3(
        [
            "2024/01/01/KLOT/KLOT20240101_000000_V06",
            "2024/01/01/KLOT/KLOT20240101_000500_V06",
            "2024/01/01/KLOT/KLOT20240101_000500_V06_MDM",
            "2024/01/01/KLOT/NOT_A_KLOT_FILE",
            "2024/01/01/KLOT/KLOT20240101_001000_V06",
        ]
    )
    monkeypatch.setattr(cloud, "_s3", lambda: client)
    cloud._DAY_CACHE.clear()
    monkeypatch.setattr(cloud, "_ARCO_STATIONS", None)
    monkeypatch.setattr(cloud, "_ARCO_STATIONS_FAILED_AT", 0.0)
    return client


class TestListFiles:
    def test_filters_sorts_and_builds_paths(self, fake_s3: FakeS3) -> None:
        files = cloud.list_nexrad_l2_files("klot", "2024-01-01")
        assert [f["name"] for f in files] == [
            "KLOT20240101_000000_V06",
            "KLOT20240101_000500_V06",
            "KLOT20240101_001000_V06",
        ]
        assert (
            files[0]["path"] == "s3://unidata-nexrad-level2/2024/01/01/KLOT/KLOT20240101_000000_V06"
        )
        assert fake_s3.calls[0]["Prefix"] == "2024/01/01/KLOT/"

    def test_past_days_are_cached_and_callers_cannot_corrupt_cache(self, fake_s3: FakeS3) -> None:
        first = cloud.list_nexrad_l2_files("KLOT", "2024-01-01")
        first[0]["name"] = "mutated"
        first.pop()
        second = cloud.list_nexrad_l2_files("KLOT", "2024-01-01")
        assert len(fake_s3.calls) == 1
        assert len(second) == 3
        assert second[0]["name"] == "KLOT20240101_000000_V06"

    def test_use_cache_false_refetches(self, fake_s3: FakeS3) -> None:
        cloud.list_nexrad_l2_files("KLOT", "2024-01-01")
        cloud.list_nexrad_l2_files("KLOT", "2024-01-01", use_cache=False)
        assert len(fake_s3.calls) == 2

    def test_today_expires(self, fake_s3: FakeS3, monkeypatch: pytest.MonkeyPatch) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        cloud.list_nexrad_l2_files("KLOT", today)
        cloud.list_nexrad_l2_files("KLOT", today)
        assert len(fake_s3.calls) == 1
        monkeypatch.setattr(cloud.time, "monotonic", lambda: 1e12)  # TTL elapsed
        cloud.list_nexrad_l2_files("KLOT", today)
        assert len(fake_s3.calls) == 2

    def test_empty_past_day_not_cached_forever(self, fake_s3: FakeS3) -> None:
        cloud.list_nexrad_l2_files("KATX", "2024-01-01")  # no keys for KATX
        key = ("KATX", "2024-01-01")
        assert cloud._DAY_CACHE[key][0] is not None  # has an expiry

    def test_errors_wrapped_not_cached(self, fake_s3: FakeS3) -> None:
        fake_s3.fail = True
        with pytest.raises(RuntimeError, match="Could not list files for KLOT"):
            cloud.list_nexrad_l2_files("KLOT", "2024-01-01")
        fake_s3.fail = False
        assert len(cloud.list_nexrad_l2_files("KLOT", "2024-01-01")) == 3

    @pytest.mark.parametrize("station", ["", "K", "KLOT/../x", "KLOTX", "kl t"])
    def test_invalid_station_rejected_before_any_s3_call(
        self, fake_s3: FakeS3, station: str
    ) -> None:
        with pytest.raises(ValueError, match="Invalid station"):
            cloud.list_nexrad_l2_files(station, "2024-01-01")
        assert fake_s3.calls == []

    @pytest.mark.parametrize("date", ["2024-13-01", "01/02/2024", "", "2024-1-1x"])
    def test_invalid_date_message(self, fake_s3: FakeS3, date: str) -> None:
        with pytest.raises(ValueError, match="expected YYYY-MM-DD"):
            cloud.list_nexrad_l2_files("KLOT", date)

    def test_get_latest_falls_back_to_yesterday_and_reports_errors(self, fake_s3: FakeS3) -> None:
        yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y/%m/%d")
        fake_s3.keys = [
            f"{yesterday}/KLOT/KLOT20240101_000000_V06",
            f"{yesterday}/KLOT/KLOT20240101_000500_V06",
        ]
        latest = cloud.get_latest_nexrad_l2("KLOT")
        assert latest["name"] == "KLOT20240101_000500_V06"

        fake_s3.keys = []
        cloud._DAY_CACHE.clear()
        with pytest.raises(RuntimeError, match="No recent Level II files"):
            cloud.get_latest_nexrad_l2("KLOT")
        fake_s3.fail = True
        with pytest.raises(RuntimeError, match="boom"):
            cloud.get_latest_nexrad_l2("KLOT")


class TestArcoStations:
    def test_listing_is_cached(self, fake_s3: FakeS3) -> None:
        assert cloud._get_arco_stations() == {"KLOT", "KATX"}
        cloud._get_arco_stations()
        assert len(fake_s3.calls) == 1
        stations = {s["id"]: s["arco"] for s in cloud.list_nexrad_stations()}
        assert stations["KLOT"] is True and stations["KTLX"] is False

    def test_failure_is_not_cached_forever(
        self, fake_s3: FakeS3, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_s3.fail = True
        assert cloud._get_arco_stations() == {"KLOT"}  # fallback
        n_calls = len(fake_s3.calls)
        cloud._get_arco_stations()  # inside the retry window: no hammering S3
        assert len(fake_s3.calls) == n_calls
        fake_s3.fail = False
        monkeypatch.setattr(cloud, "_ARCO_STATIONS_FAILED_AT", 0.0001)  # long ago
        assert cloud._get_arco_stations() == {"KLOT", "KATX"}


class TestArcoScans:
    def test_list_arco_scans_uses_group_listing_and_limit(self, fake_arco) -> None:
        scans = cloud.list_arco_scans("klot")
        assert scans == [
            {"key": ARCO_SCAN_KEY, "path": f"icechunk://nexrad-arco/KLOT/{ARCO_SCAN_KEY}"}
        ]
        assert cloud.list_arco_scans("KLOT", limit=0) == scans

    def test_bad_station(self) -> None:
        with pytest.raises(ValueError, match="Invalid station"):
            cloud.list_arco_scans("../etc")


class TestClient:
    def test_client_is_shared_and_has_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cloud, "_S3_CLIENT", None)
        a = cloud._s3()
        b = cloud._s3()
        assert a is b
        cfg = a.meta.config
        assert cfg.connect_timeout == cloud._CONNECT_TIMEOUT_S
        assert cfg.read_timeout == cloud._READ_TIMEOUT_S


class TestStationTable:
    def test_every_station_has_valid_coordinates(self, fake_s3: FakeS3) -> None:
        assert set(cloud._NEXRAD_SITES) == set(cloud._NEXRAD_STATIONS)
        for s in cloud.list_nexrad_stations():
            assert {"id", "arco", "latitude", "longitude", "elevation_m"} <= set(s)
            assert 10 < s["latitude"] < 72
            assert -180 < s["longitude"] < 180
            assert -5 <= s["elevation_m"] < 4000

    def test_known_sites(self) -> None:
        lat, lon, _ = cloud._NEXRAD_SITES["KTLX"]
        assert (lat, lon) == pytest.approx((35.333, -97.278), abs=0.01)
        lat, lon, _ = cloud._NEXRAD_SITES["KATX"]
        assert (lat, lon) == pytest.approx((48.195, -122.496), abs=0.01)


class TestNearbyStations:
    # Chicago (KLOT) centre
    LAT, LON = 41.60444, -88.08444

    def test_sorted_by_distance_and_excludes_centre(self) -> None:
        res = cloud.nearby_stations(self.LAT, self.LON, 300, 5, exclude="klot")
        assert [r["id"] for r in res][:3] == ["KMKX", "KILX", "KIWX"]
        assert "KLOT" not in [r["id"] for r in res]
        dists = [r["distance_km"] for r in res]
        assert dists == sorted(dists)
        assert all(d <= 300 for d in dists)
        assert len(res) == 5

    def test_distance_and_bearing(self) -> None:
        res = {r["id"]: r for r in cloud.nearby_stations(self.LAT, self.LON, 300, 0)}
        assert res["KMKX"]["distance_km"] == pytest.approx(156.3, abs=1.0)
        assert res["KMKX"]["bearing_deg"] == pytest.approx(346, abs=2)  # NNW
        assert res["KDVN"]["bearing_deg"] == pytest.approx(271, abs=2)  # W
        assert 0 <= res["KIWX"]["bearing_deg"] < 360

    def test_radius_limit_and_zero_limit(self) -> None:
        assert cloud.nearby_stations(self.LAT, self.LON, 1, 3) == [
            {
                "id": "KLOT",
                "latitude": 41.60444,
                "longitude": -88.08444,
                "distance_km": 0.0,
                "bearing_deg": 0.0,
            }
        ]
        assert cloud.nearby_stations(self.LAT, self.LON, 1, 3, exclude="KLOT") == []
        many = cloud.nearby_stations(self.LAT, self.LON, 2000, 0)
        assert len(many) > 20

    def test_invalid_coordinates(self) -> None:
        with pytest.raises(ValueError, match="Invalid coordinates"):
            cloud.nearby_stations(95.0, 0.0, 100)


class TestNearestScan:
    FILES: ClassVar[list[dict[str, str]]] = [
        {"name": "KLOT20240101_000000_V06"},
        {"name": "KLOT20240101_000500_V06"},
        {"name": "KLOT20240101_001000_V06"},
        {"name": "KLOT20240101_001500_V06_MDM"},
        {"name": "garbage"},
    ]

    def test_picks_closest(self) -> None:
        t = datetime(2024, 1, 1, 0, 3, tzinfo=UTC)
        assert cloud.nearest_scan_to_time(self.FILES, t)["name"] == "KLOT20240101_000500_V06"

    def test_naive_target_is_utc_and_tie_goes_earlier(self) -> None:
        got = cloud.nearest_scan_to_time(self.FILES, datetime(2024, 1, 1, 0, 2, 30))
        assert got["name"] == "KLOT20240101_000000_V06"

    def test_max_gap(self) -> None:
        t = datetime(2024, 1, 1, 5, 0)
        assert cloud.nearest_scan_to_time(self.FILES, t) is not None
        assert cloud.nearest_scan_to_time(self.FILES, t, max_gap=timedelta(minutes=30)) is None

    def test_ignores_mdm_files(self) -> None:
        t = datetime(2024, 1, 1, 0, 16)
        assert cloud.nearest_scan_to_time(self.FILES, t)["name"] == "KLOT20240101_001000_V06"

    def test_empty_and_unparsable(self) -> None:
        assert cloud.nearest_scan_to_time([], datetime(2024, 1, 1)) is None
        assert cloud.nearest_scan_to_time([{"name": "x"}], datetime(2024, 1, 1)) is None

    def test_scan_time(self) -> None:
        assert cloud.scan_time("2024/01/01/KLOT/KLOT20240101_000500_V06") == datetime(
            2024, 1, 1, 0, 5, tzinfo=UTC
        )
        assert cloud.scan_time("KLOT20241399_000500_V06") is None


class TestLatestScanInfo:
    """Radar-sites card: newest volume of a station, from a mocked S3 listing (no network)."""

    @pytest.fixture(autouse=True)
    def _clean(self, fake_s3: FakeS3) -> None:
        cloud._INFO_CACHE.clear()

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).strftime("%Y/%m/%d")

    def test_ok_reports_newest_volume_size_and_age(self, fake_s3: FakeS3) -> None:
        now = datetime.now(UTC)
        older = (now - timedelta(minutes=14)).strftime("%Y%m%d_%H%M%S")
        newer = (now - timedelta(minutes=4)).strftime("%Y%m%d_%H%M%S")
        fake_s3.keys = [
            f"{self._today()}/KLOT/KLOT{older}_V06",
            f"{self._today()}/KLOT/KLOT{newer}_V06",
            f"{self._today()}/KLOT/KLOT{newer}_V06_MDM",
        ]
        info = cloud.latest_scan_info("klot")
        assert info["status"] == "ok"
        assert info["station"] == "KLOT"
        assert info["name"] == f"KLOT{newer}_V06"
        assert info["path"] == f"s3://unidata-nexrad-level2/{self._today()}/KLOT/KLOT{newer}_V06"
        assert info["size"] == 10
        assert info["scan_time"].endswith("Z")
        assert 200 < info["age_s"] < 400  # ~4 min, from the file name (the volume start time)
        assert info["now"].endswith("Z")
        assert info["source"] == "s3://unidata-nexrad-level2/"

    def test_falls_back_to_yesterday(self, fake_s3: FakeS3) -> None:
        y = datetime.now(UTC) - timedelta(days=1)
        fake_s3.keys = [f"{y:%Y/%m/%d}/KLOT/KLOT{y:%Y%m%d}_235500_V06"]
        info = cloud.latest_scan_info("KLOT")
        assert info["status"] == "ok"
        assert info["scan_time"].startswith(f"{y:%Y-%m-%d}T23:55:00")
        assert info["age_s"] > 0

    def test_not_found_has_no_age_and_is_not_an_error(self, fake_s3: FakeS3) -> None:
        fake_s3.keys = []
        info = cloud.latest_scan_info("KLOT")
        assert info["status"] == "not_found"
        assert "age_s" not in info and "scan_time" not in info

    def test_offline(self, fake_s3: FakeS3, monkeypatch: pytest.MonkeyPatch) -> None:
        class EndpointConnectionError(Exception):  # same class name as botocore's
            pass

        class Boom:
            def get_paginator(self, name: str):
                raise EndpointConnectionError("no route")

        monkeypatch.setattr(cloud, "_s3", lambda: Boom())
        info = cloud.latest_scan_info("KLOT", use_cache=False)
        assert info["status"] == "offline"
        assert "age_s" not in info

    def test_connection_error_is_offline_other_errors_are_not(
        self, fake_s3: FakeS3, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Err:
            def __init__(self, exc: Exception) -> None:
                self.exc = exc

            def get_paginator(self, name: str):
                raise self.exc

        monkeypatch.setattr(cloud, "_s3", lambda: Err(ConnectionError("reset")))
        assert cloud.latest_scan_info("KLOT", use_cache=False)["status"] == "offline"
        monkeypatch.setattr(cloud, "_s3", lambda: Err(PermissionError("denied")))
        info = cloud.latest_scan_info("KLOT", use_cache=False)
        assert info["status"] == "error"
        assert "denied" in info["message"]

    def test_successes_are_cached_briefly(self, fake_s3: FakeS3) -> None:
        stamp = f"{datetime.now(UTC):%Y%m%d_%H%M%S}"
        fake_s3.keys = [f"{self._today()}/KLOT/KLOT{stamp}_V06"]
        cloud.latest_scan_info("KLOT")
        n = len(fake_s3.calls)
        cloud.latest_scan_info("KLOT")
        assert len(fake_s3.calls) == n  # second call served from the 20 s cache
        cloud.latest_scan_info("KLOT", use_cache=False)
        assert len(fake_s3.calls) > n

    def test_failures_are_not_cached(self, fake_s3: FakeS3) -> None:
        fake_s3.fail = True
        assert cloud.latest_scan_info("KLOT")["status"] == "error"
        assert "KLOT" not in cloud._INFO_CACHE

    def test_bad_station_never_touches_s3(self, fake_s3: FakeS3) -> None:
        info = cloud.latest_scan_info("../x")
        assert info["status"] == "error"
        assert fake_s3.calls == []

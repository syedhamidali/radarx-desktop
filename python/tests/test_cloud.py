"""Tests for engine.cloud (S3 listing caches, validation, ARCO helpers) — no network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import engine.cloud as cloud
from tests.conftest import ARCO_SCAN_KEY

_MODIFIED = datetime(2024, 1, 1, tzinfo=timezone.utc)


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
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y/%m/%d")
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

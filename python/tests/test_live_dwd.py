"""The live map with two networks: NEXRAD and DWD. No network: a fake DWD provider and synthetic
volumes; the real listing and download of DWD were exercised by hand (see docs/LIVE.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

import engine.live as live
from engine import mosaic, radar_networks
from tests.test_live import FakeReader, needs_radarx
from tests.test_mosaic import flat, make_reader, make_volume

DWD_NEWEST = {
    "status": "ok",
    "provider": "dwd",
    "site": "ess",
    "key": "202610220925",
    "scan_time": "2026-10-22T09:25:57Z",
    "name": "DWD vol5minng01 2026-10-22 09:25 UTC",
    "size": 2374021,
    "n_files": 20,
    "sweeps": 10,
    "complete": True,
    "files": [{"name": "x"}],
    "now": "2026-10-22T09:34:34Z",
    "age_s": 517.0,
}


class FakeDwd(radar_networks.DwdProvider):
    def latest_scan_info(self, site, *, use_cache=True):
        if site == "fld":
            return {"status": "not_found", "provider": "dwd", "site": site, "message": "none"}
        return {**DWD_NEWEST, "site": site}


@pytest.fixture(autouse=True)
def fake_dwd(monkeypatch: pytest.MonkeyPatch) -> None:
    prov = FakeDwd()
    real = radar_networks.get_provider
    monkeypatch.setattr(
        radar_networks, "get_provider", lambda pid: prov if str(pid).lower() == "dwd" else real(pid)
    )


class TestIds:
    def test_a_bare_four_letter_id_is_nexrad_and_unchanged(self) -> None:
        assert live.parse_site_id("ktlx") == ("KTLX", "nexrad", "KTLX")
        assert live.parse_site_id("nexrad:kvnx") == ("KVNX", "nexrad", "KVNX")

    def test_dwd_ids_are_qualified_and_lower_case(self) -> None:
        assert live.parse_site_id("dwd:ESS") == ("dwd:ess", "dwd", "ess")
        assert live.site_key("dwd:ESS") == "dwd:ess" and live.site_key("ktlx") == "KTLX"

    def test_other_networks_and_unknown_sites_are_refused_by_name(self) -> None:
        with pytest.raises(ValueError, match="not available on the live map"):
            live.parse_site_id("fmi:vimpeli")
        with pytest.raises(ValueError, match="not available on the live map"):
            live.parse_site_id("saws:pretoria")
        with pytest.raises(ValueError):
            live.parse_site_id("dwd:zzz")

    def test_a_mixed_list_keeps_order_and_drops_duplicates(self) -> None:
        got = live.normalize_stations(["ktlx", "dwd:ess", "DWD:ess", "KTLX", "dwd:fld"])
        assert got == ["KTLX", "dwd:ess", "dwd:fld"]


class TestLatest:
    def test_same_shape_for_both_networks(self) -> None:
        info = live.latest("dwd:ess")
        assert info["station"] == "dwd:ess" and info["provider"] == "dwd"
        assert info["key"] == "202610220925" and info["size"] == 2374021
        assert info["name"] == "DWD ESS vol5minng01 2026-10-22 09:25 UTC"
        assert "files" not in info and info["status"] == "ok"
        assert live.latest("dwd:fld")["status"] == "not_found"
        assert live.latest("nope!")["status"] == "error"

    def test_check_reports_held_for_a_dwd_volume(self) -> None:
        c = live.LiveCache()
        v = live.HeldVolume(
            "dwd:ess", live.latest("dwd:ess")["name"], "dwd:ess:202610220925", 5, None, FakeReader("x"),
            provider="dwd",
        )  # fmt: skip
        c.hold(v)
        res = live.check(["dwd:ess"], cache=c)
        assert res[0]["held"] is True


def _reader_at(lat: float, lon: float, name: str):
    reader = make_reader(make_volume(lat, lon, flat(30.0), name=name, n_range=60))
    reader._path = f"src/{name}"
    return reader


@needs_radarx
class TestMixedUpdate:
    def _files(self):
        return [
            {"station": "KTLX", "path": "s3://b/a", "name": "a", "size": 5},
            {
                "station": "dwd:ess",
                "key": "202610220925",
                "name": DWD_NEWEST["name"],
                "size": 2374021,
                "scan_time": DWD_NEWEST["scan_time"],
            },
        ]

    def test_a_composite_of_both_networks_carries_both_credits(self) -> None:
        c = live.LiveCache()
        got: list[tuple[str, str]] = []

        def dwd_opener(site: str, key: str):
            got.append((site, key))
            return _reader_at(51.4, 6.97, "DWD ESS")

        rec, radars = live.update(
            self._files(),
            cache=c,
            opener=lambda p: _reader_at(35.3, -97.3, "KTLX"),
            dwd_opener=dwd_opener,
            grid={"resolution_m": 4000.0},
        )
        assert got == [("ess", "202610220925")]
        assert [r["provider"] for r in radars] == ["nexrad", "dwd"]
        assert c.get("dwd:ess").provider == "dwd" and c.get("KTLX").provider == "nexrad"
        attrs = rec.extra_attrs
        assert "NOAA" in attrs["data_credit"] and "Deutscher Wetterdienst" in attrs["data_credit"]
        assert "CC BY 4.0" in attrs["data_licence"] and "public domain" in attrs["data_licence"]
        header = live.frame_header(rec, radars, cache=c)
        assert [x["source"] for x in header["credits"]] == [x["source"] for x in rec._live_credits]
        assert len(header["credits"]) == 2

    def test_exports_say_where_dwd_data_came_from(self, tmp_path: Path) -> None:
        import xarray as xr

        rec, _r = live.update(
            [self._files()[1]],
            cache=live.LiveCache(),
            dwd_opener=lambda s, k: _reader_at(51.4, 6.97, "DWD ESS"),
            grid={"resolution_m": 4000.0},
        )
        nc = tmp_path / "x.nc"
        mosaic.write_netcdf(rec, nc)
        with xr.open_dataset(nc) as ds:
            assert "Deutscher Wetterdienst" in ds.attrs["data_credit"]
            assert "CC BY 4.0" in ds.attrs["data_licence"]
            assert "NOAA" not in ds.attrs["data_credit"]
        # a redraw keeps the credit
        again = live.restyle(rec.mosaic_id, None)
        assert "Deutscher Wetterdienst" in again.extra_attrs["data_credit"]

    def test_a_dwd_item_needs_its_volume_key(self) -> None:
        with pytest.raises(live.LiveError, match="12-digit"):
            live.update(
                [{"station": "dwd:ess", "name": "n", "key": "../x"}], cache=live.LiveCache()
            )
        with pytest.raises(live.LiveError, match="not available on the live map"):
            live.update(
                [{"station": "fmi:x", "name": "n", "key": "202610220925"}], cache=live.LiveCache()
            )


class TestMemoryPin:
    def _held(self) -> live.HeldVolume:
        reader = _reader_at(51.4, 6.97, "DWD ESS")
        reader._path = "https://opendata.dwd.de/x/dwd-ess-vol5minng01-202610220925"
        return live.HeldVolume(
            "dwd:ess", DWD_NEWEST["name"], "dwd:ess:202610220925", 2374021, None, reader,
            provider="dwd",
        )  # fmt: skip

    def test_a_dwd_volume_is_openable_without_a_file_and_costs_no_disk(self) -> None:
        h = self._held()
        assert h.local_path is None and h.openable is True and h.as_dict()["on_disk"] is False
        reg = live.PinRegistry()
        c = live.LiveCache()
        c.hold(h)
        (pin,) = live.pin_volumes(["dwd:ess"], cache=c, pins=reg)
        assert pin.path is None and pin.size == 0 and pin.volume_size == 2374021
        assert pin.as_dict()["kind"] == "memory"
        assert reg.status()["bytes"] == 0 and len(reg) == 1
        # it survives the cache letting go of the volume, and sharing the tree is not a copy
        c.clear()
        reader, schema = live.open_pinned(pin)
        assert reader.datatree is pin.tree
        assert schema["source_type"] == "dwd_opendata" and schema["source"] == pin.source
        assert "Deutscher Wetterdienst" in reader._source_credit["attribution"]
        assert live.release_reader(reader, pins=reg) is True and len(reg) == 0

    def test_a_volume_without_data_is_not_openable(self) -> None:
        h = live.HeldVolume("dwd:ess", "n", "p", 1, None, FakeReader("x"), provider="dwd")
        assert h.openable is False
        c = live.LiveCache()
        c.hold(h)
        with pytest.raises(live.LiveError, match="Update now"):
            live.pin_volumes(["dwd:ess"], cache=c, pins=live.PinRegistry())

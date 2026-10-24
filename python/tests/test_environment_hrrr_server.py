"""``env_profile``: a model column through the WebSocket, from the cache only.

The sidecar's default environment is pointed at a temporary folder and at the in-memory HRRR
bucket of ``tests.env_synth``; nothing touches the network.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("radarx")

from engine.environment import Environment
from tests import env_synth as es
from tests.test_environment_hrrr_profile import column_bucket
from tests.test_environment_server import ask, make_handler

pytestmark = pytest.mark.skipif(not es.have_eccodes(), reason="ecCodes (extra 'grib') missing")

REQUEST = {"provider": "hrrr", "product": "profile", "time_start": "2021-12-11T03:00:00Z"}


def profile(handler, rid, **where):
    return ask(handler, type="env_profile", request_id=rid, request=REQUEST, **where)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from engine import sounding

    monkeypatch.setenv("XRADAR_ENV_SETTINGS", str(tmp_path / "environment.json"))
    monkeypatch.setenv("XRADAR_ENV_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("XRADAR_OFFLINE", raising=False)
    Environment.reset_default()
    env = Environment.default()
    env.http._opener = column_bucket(sounding.weisman_klemp()).opener
    env.http.min_interval_s = 0
    yield
    Environment.reset_default()


def test_a_profile_needs_the_users_approval_first():
    h = make_handler()
    out = asyncio.run(profile(h, "p1", lat=35.2, lon=-93.6))
    assert out[-1]["type"] == "sounding_error"
    assert "approval" in out[-1]["message"] and "Nothing was downloaded" in out[-1]["message"]


def test_estimate_approve_fetch_then_the_column_is_a_labelled_sounding():
    async def scenario():
        h = make_handler()
        est = await ask(h, type="env_estimate", request_id="r1", request=REQUEST)
        e = est[-1]["estimate"]
        assert est[-1]["type"] == "env_estimate_result" and e["size_exact"]
        assert e["n_files"] == 6 + 5 * 19 and "noaa-hrrr-bdp-pds" in e["files"][0]["url"]
        done = await ask(h, type="env_fetch", request_id="r1", approve=True)
        assert done[-1]["type"] == "env_fetch_result"
        assert done[-1]["result"]["bytes_downloaded"] == e["network_bytes"]
        return await profile(h, "p2", lat=35.2, lon=-93.6)

    out = asyncio.run(scenario())
    assert out[-1]["type"] == "sounding_result", out[-1]
    snd = out[-1]["sounding"]
    assert snd["data_class"] == "model" and snd["format"] == "hrrr_grib2"
    prov = snd["provenance"]
    assert prov["kind"] == "model" and prov["run_time"].startswith("2021-12-11T03")
    assert snd["diagnostics"]["parcels"]["surface-based"]["cape"] > 1000


def test_bad_lat_lon_is_an_error_not_a_crash():
    h = make_handler()
    out = asyncio.run(profile(h, "p3", lat="north", lon=1))
    assert out[-1]["type"] == "sounding_error" and "lat and lon" in out[-1]["message"]

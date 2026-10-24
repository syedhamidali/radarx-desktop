"""``rxd env``: consent, estimate, fetch, show, profile and cache from the command line.

The provider is HRRR on the in-memory bucket of ``tests.env_synth`` (synthetic GRIB2), the process
wide environment is replaced by a throw-away one, so no network and no real cache is touched.
"""

from __future__ import annotations

import json

import pytest

from engine import cli
from engine.environment import Environment, EnvSettings, HttpClient, Registry, Request
from engine.environment.providers import hrrr
from tests import env_synth as es

pytestmark = pytest.mark.skipif(not es.have_eccodes(), reason="ecCodes (extra 'grib') missing")

ARGS = ("hrrr", "--time", "2021-12-11T03:00Z", "--bbox=-93.8,35,-93.4,35.3", "-V", "cape,cin")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    bucket = es.make_hrrr_bucket(es.run_time(3))
    http = HttpClient(opener=bucket.opener, min_interval_s=0, backoff_s=0, sleep=lambda s: None)
    e = Environment(
        EnvSettings(cache_dir=str(tmp_path / "cache"), cache_max_mb=64),
        http=http,
        registry=Registry(),
    )
    e.registry.register(hrrr.HrrrProvider())
    e.bucket = bucket  # type: ignore[attr-defined]
    monkeypatch.setattr(Environment, "_default", e)
    monkeypatch.chdir(tmp_path)
    return e


def run(capsys, *argv):
    code = cli.main(["env", *map(str, argv)])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_providers_and_inventory(env, capsys):
    code, out, _ = run(capsys, "providers")
    assert code == 0 and "hrrr" in out and "model" in out
    code, out, _ = run(capsys, "inventory", "hrrr")
    assert code == 0 and "surface" in out and "pressure" in out and "profile" in out
    code, out, _ = run(capsys, "inventory", "hrrr", "--json")
    assert {i["product"] for i in json.loads(out)["inventory"]} >= {"surface", "native"}


def test_estimate_prints_the_consent_text_and_downloads_nothing(env, capsys):
    code, out, _ = run(capsys, "estimate", *ARGS)
    assert code == 0
    for token in ("From      noaa-hrrr-bdp-pds.s3.amazonaws.com", "Download  2 file(s)", "Saved to",
                  "Credit", "Licence", "analysis", "[MODEL]"):  # fmt: skip
        assert token in out, token
    assert not [r for _m, _u, r in env.bucket.log if r]  # no byte range was fetched
    code, out, _ = run(capsys, "estimate", *ARGS, "--json")
    est = json.loads(out)["estimate"]
    assert est["size_exact"] and est["network_bytes"] > 0 and est["requires_approval"]


def test_fetch_without_yes_downloads_nothing_and_says_how_to_approve(env, capsys):
    code, _out, err = run(capsys, "fetch", *ARGS)
    assert code == 2 and "Nothing was downloaded" in err and "--yes" in err
    assert "noaa-hrrr-bdp-pds.s3.amazonaws.com" in err  # the server is named
    assert not [r for _m, _u, r in env.bucket.log if r]


def test_fetch_yes_downloads_exactly_the_estimate_and_a_repeat_is_free(env, capsys):
    code, out, _err = run(capsys, "fetch", *ARGS, "--yes")
    assert code == 0 and "2 file(s) ready (2 downloaded" in out
    again = Request(
        provider="hrrr",
        product="surface",
        variables=("cape", "cin"),
        time_start="2021-12-11T03:00:00Z",
        bbox=(-93.8, 35, -93.4, 35.3),
    )
    est = env.estimate(again)
    assert est.network_bytes == 0 and est.cached_bytes > 0
    n = len(env.bucket.log)
    code, out, _ = run(capsys, "fetch", *ARGS)  # cached: no question, no network
    assert code == 0 and "0 downloaded" in out and len(env.bucket.log) == n


def test_show_writes_a_netcdf_with_the_provenance(env, capsys, tmp_path):
    xr = pytest.importorskip("xarray")
    code, out, _ = run(capsys, "show", *ARGS, "--yes", "-o", "env.nc")
    assert code == 0 and "[model]" in out and "cape" in out
    ds = xr.open_dataset(tmp_path / "env.nc")
    assert ds.attrs["data_class"] == "model" and "HRRR" in ds.attrs["data_source"]
    assert ds["cape"].shape[0] > 0


def test_show_json_and_bad_output_suffix(env, capsys):
    code, out, _ = run(capsys, "show", *ARGS, "--yes", "--json")
    assert code == 0 and json.loads(out)["attrs"]["data_class"] == "model"
    code, _o, err = run(capsys, "show", *ARGS, "-o", "x.csv")
    assert code == 2 and ".nc or .json" in err


def test_unknown_names_are_errors_not_guesses(env, capsys):
    bad = ("hrrr", "--time", "2021-12-11T03:00Z", "-V", "TMP:3 m above ground")
    code, _o, err = run(capsys, "estimate", *bad)
    assert code == 2 and "not in the index" in err
    code, _o, err = run(capsys, "estimate", "nope", "--time", "2021-12-11T03:00Z", "-V", "cape")
    assert code == 2 and "No environment-data provider named 'nope'" in err
    code, _o, err = run(capsys, "estimate", "hrrr", "--time", "2021-12-11T03:00Z")
    assert code == 2 and "Name the variables" in err


def test_cache_settings_offline_and_clear(env, capsys):
    run(capsys, "fetch", *ARGS, "--yes")
    code, out, _ = run(capsys, "cache")
    # two messages and the remembered listing that lets the estimate work offline
    assert code == 0 and "cache folder" in out and "in 3 file(s)" in out
    code, out, _ = run(capsys, "cache", "--offline")
    assert code == 0 and "offline mode  on" in out
    other = ("hrrr", "--time", "2021-12-11T03:00Z", "-V", "t2m", "--yes")
    code, _o, err = run(capsys, "fetch", *other)
    assert code == 3 and "ffline" in err  # a missing file needs the network
    code, out, _ = run(capsys, "cache", "--online", "--clear")
    assert code == 0 and "removed" in out and "offline mode  off" in out
    code, _o, err = run(capsys, "cache", "--online", "--offline")
    assert code == 2


def test_the_overview_lists_env():
    assert "env" in cli.overview()

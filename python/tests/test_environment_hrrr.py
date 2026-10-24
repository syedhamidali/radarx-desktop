"""The HRRR provider: index parsing, byte-range planning, consent, cache, decoding, profile.

No network: the bucket is ``tests.env_synth.Bucket`` (an in-memory server with Range support) and
the GRIB2 messages are made with ecCodes. Decoding tests skip when ecCodes is not installed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from engine.environment import (
    ApprovalRequired,
    Environment,
    EnvSettings,
    FetchFailed,
    HttpClient,
    InvalidRequest,
    OfflineError,
    Registry,
    Request,
)
from engine.environment.providers import hrrr
from tests import env_synth as es

needs_eccodes = pytest.mark.skipif(not es.have_eccodes(), reason="ecCodes (extra 'grib') missing")
RUN = es.run_time(3)


def make_env(tmp_path, bucket, *, offline=False):
    http = HttpClient(opener=bucket.opener, min_interval_s=0, backoff_s=0, sleep=lambda s: None)
    env = Environment(
        EnvSettings(cache_dir=str(tmp_path / "cache"), cache_max_mb=64, offline=offline),
        http=http,
        registry=Registry(),
    )
    env.registry.register(hrrr.HrrrProvider())
    return env


def req(variables=("cape", "cin"), **kw):
    kw.setdefault("time_start", "2021-12-11T02:50:00Z")
    kw.setdefault("bbox", (-93.8, 35.0, -93.4, 35.3))
    return Request(provider="hrrr", product="surface", variables=tuple(variables), **kw)


# -- pure logic -------------------------------------------------------------------------------


def test_parse_idx_gives_byte_ranges_and_never_guesses_the_last_one():
    text = (
        "1:0:d=2021121103:REFC:entire atmosphere:anl:\n"
        "2:1000:d=2021121103:TMP:2 m above ground:anl:\n"
    )
    rows = hrrr.parse_idx(text, 5000)
    assert [(r.offset, r.end, r.size) for r in rows] == [(0, 999, 1000), (1000, 4999, 4000)]
    without = hrrr.parse_idx(text, None)  # file size unknown: the last range is not invented
    assert [r.name for r in without] == ["REFC"]
    with pytest.raises(InvalidRequest):
        hrrr.parse_idx("nothing useful\n", 10)
    with pytest.raises(InvalidRequest):
        hrrr.parse_idx("1:50:d=x:A:b:anl:\n2:10:d=x:C:d:anl:\n", 100)


def test_variable_tokens_aliases_bundles_and_errors():
    out = hrrr.expand_variables(("cape", "CAPE", "TMP:500 mb", "@storm_environment"))
    tokens = [t for t, _ in out]
    assert tokens.count("cape") == 1 and "TMP:500 mb" in tokens and "srh_0_3km" in tokens
    assert dict(out)["cape"] == "cape" and dict(out)["TMP:500 mb"] is None
    with pytest.raises(InvalidRequest, match="Unknown set"):
        hrrr.expand_variables(("@nonsense",))
    with pytest.raises(InvalidRequest, match="NAME:level"):
        hrrr._token_parts("TMP", None)


def test_match_rows_exact_words_wildcards_and_ambiguity():
    text = "\n".join(
        [
            "1:0:d=2021121103:REFD:263 K level:anl:",
            "2:10:d=2021121103:TMP:850 mb:anl:",
            "3:20:d=2021121103:TMP:700 mb:anl:",
            "4:30:d=2021121103:TMP:2 m above ground:anl:",
            "5:40:d=2021121103:REFD:263 K level:0-1 hour max fcst:",
            "6:50:d=2021121103:CLMR:1 hybrid level:anl:",
            "7:60:d=2021121103:CLMR:2 hybrid level:anl:",
            "8:70:d=2021121103:HGT:surface:anl:",
        ]
    )
    rows = hrrr.parse_idx(text, 100)
    assert [r.level for r in hrrr.match_rows(rows, "TMP:@mb", None, "pressure")] == [
        "850 mb",
        "700 mb",
    ]
    assert len(hrrr.match_rows(rows, "CLMR:@hybrid", None, "native")) == 2
    # the instantaneous one is chosen when only one of the repeated rows is
    assert hrrr.match_rows(rows, "REFD:263 K level", None, "surface")[0].step == "anl"
    assert hrrr.match_rows(rows, "REFD:263 K level:0-1 hour max fcst", None, "surface")[0].n == 5
    with pytest.raises(InvalidRequest, match="not in the index") as exc:
        hrrr.match_rows(rows, "TMP:100 mb", None, "pressure")
    assert "850 mb" in str(exc.value)  # lists what exists, never guesses
    with pytest.raises(InvalidRequest, match="not in this file at all"):
        hrrr.match_rows(rows, "NOPE:surface", None, "pressure")
    both = hrrr.parse_idx(
        "1:0:d=x:APCP:surface:0-1 hour acc fcst:\n2:10:d=x:APCP:surface:0-3 hour acc fcst:\n", 20
    )
    with pytest.raises(InvalidRequest, match="Say which one"):
        hrrr.match_rows(both, "APCP:surface", None, "surface")


def test_selection_nearest_hour_and_states_the_offset():
    sel = hrrr.Selection.of(req(time_start="2021-12-11T02:50:00Z"))
    assert (sel.run, sel.fxx, sel.valid) == (RUN, 0, RUN)
    assert sel.offset_s == pytest.approx(600)
    assert "+10 min" in sel.describe() and "not interpolated" in sel.describe()
    late = hrrr.Selection.of(req(time_start="2021-12-11T03:31:00Z"))
    assert late.valid == datetime(2021, 12, 11, 4, tzinfo=UTC)
    f2 = hrrr.Selection.of(req(time_start="2021-12-11T05:00:00Z", params={"fxx": 2}))
    assert (f2.run, f2.fxx) == (RUN, 2)
    run = hrrr.Selection.of(req(time_start=None, params={"run": "2021-12-11T00:00:00Z", "fxx": 30}))
    assert run.valid == datetime(2021, 12, 12, 6, tzinfo=UTC)  # 00Z runs reach 48 h
    with pytest.raises(InvalidRequest, match="forecasts to hour 18"):
        hrrr.Selection.of(req(time_start=None, params={"run": "2021-12-11T03:00:00Z", "fxx": 30}))
    with pytest.raises(InvalidRequest, match="Give the time"):
        hrrr.Selection.of(req(time_start=None))
    with pytest.raises(InvalidRequest, match="on the hour"):
        hrrr.Selection.of(req(time_start=None, params={"run": "2021-12-11T03:20:00Z"}))


def test_a_request_without_variables_never_downloads_whole_files(tmp_path):
    env = make_env(tmp_path, es.make_hrrr_bucket(RUN) if es.have_eccodes() else es.Bucket("x"))
    with pytest.raises(InvalidRequest, match="Name the variables"):
        env.estimate(Request(provider="hrrr", product="surface", time_start="2021-12-11T03:00:00Z"))
    with pytest.raises(InvalidRequest, match="Unknown HRRR product"):
        env.estimate(
            Request(
                provider="hrrr",
                product="nope",
                variables=("cape",),
                time_start="2021-12-11T03:00:00Z",
            )
        )


# -- planning, consent, download, cache (needs eccodes only to build the fixture) ---------------


@needs_eccodes
def test_estimate_is_exact_reads_only_the_index_and_downloads_nothing(tmp_path):
    bucket = es.make_hrrr_bucket(RUN)
    env = make_env(tmp_path, bucket)
    est = env.estimate(req())
    body = bucket.files["hrrr.20211211/conus/hrrr.t03z.wrfsfcf00.grib2"]
    assert est.size_exact and est.requires_approval
    assert len(est.files) == 2 and est.total_bytes < len(body)
    # every file is a ranged piece of the one GRIB file; the sizes are the message lengths
    rows = hrrr.parse_idx(
        bucket.files["hrrr.20211211/conus/hrrr.t03z.wrfsfcf00.grib2.idx"].decode(), len(body)
    )
    assert est.total_bytes == rows[0].size + rows[1].size
    # nothing but the index (GET) and the size (HEAD) was requested
    assert [m for m, u, r in bucket.log] == ["GET", "HEAD"]
    assert bucket.log[0][1].endswith(".idx") and bucket.log[0][2] is None
    assert any("analysis" in n and "+10 min" in n for n in est.notes)
    assert any("same for any area" in n for n in est.notes)
    d = est.to_dict()
    assert d["network_bytes"] == est.total_bytes and "noaa-hrrr-bdp-pds" in d["files"][0]["url"]


@needs_eccodes
def test_fetch_needs_approval_then_downloads_exactly_the_approved_bytes_and_reuses_the_cache(
    tmp_path,
):
    bucket = es.make_hrrr_bucket(RUN)
    env = make_env(tmp_path, bucket)
    r = req()
    est = env.estimate(r)
    with pytest.raises(ApprovalRequired):
        env.fetch(r)
    assert not any(rng for _m, _u, rng in bucket.log)  # no ranged GET happened
    result = env.fetch(r, est.approve())
    ranged = [rng for m, _u, rng in bucket.log if m == "GET" and rng]
    assert len(ranged) == 2
    assert result.bytes_downloaded == est.total_bytes == sum(p.stat().st_size for p in result.paths)
    assert all(p.read_bytes()[:4] == b"GRIB" for p in result.paths)
    # a repeat is served from the cache: no network, no approval needed
    before = len(bucket.log)
    again = env.fetch(r)
    assert again.bytes_downloaded == 0 and all(again.from_cache)
    assert len(bucket.log) == before
    # a request that shares one message downloads only the new one
    r2 = req(variables=("cape", "t2m"))
    est2 = env.estimate(r2)
    new = [f.name for f, c in zip(est2.files, est2.cached, strict=True) if not c]
    assert new[0].endswith("TMP_2_m_above_ground.grib2")
    assert est2.network_bytes < est2.total_bytes


@needs_eccodes
def test_offline_reuses_the_cache_and_refuses_the_network(tmp_path):
    bucket = es.make_hrrr_bucket(RUN)
    env = make_env(tmp_path, bucket)
    r = req()
    env.fetch(r, env.estimate(r).approve())
    off = make_env(tmp_path, bucket, offline=True)
    n = len(bucket.log)
    result = off.fetch(r)  # cached and listed before: works with no network at all
    assert all(result.from_cache) and len(bucket.log) == n
    with pytest.raises(OfflineError):
        off.fetch(req(variables=("t2m",)))


@needs_eccodes
def test_a_server_that_ignores_byte_ranges_is_stopped_and_nothing_is_kept(tmp_path):
    bucket = es.make_hrrr_bucket(RUN)
    env = make_env(tmp_path, bucket)
    r = req()
    est = env.estimate(r)
    bucket.ignore_range = True
    with pytest.raises(FetchFailed):
        env.fetch(r, est.approve())
    assert not env.cache.contains("hrrr", est.files[0].key)


@needs_eccodes
def test_a_missing_run_is_a_clear_error(tmp_path):
    env = make_env(tmp_path, es.make_hrrr_bucket(RUN))
    with pytest.raises(InvalidRequest, match="no surface file"):
        env.estimate(req(time_start="2021-12-11T09:00:00Z"))


@needs_eccodes
def test_a_name_not_in_the_index_is_an_error_with_the_levels_that_exist(tmp_path):
    env = make_env(tmp_path, es.make_hrrr_bucket(RUN))
    with pytest.raises(InvalidRequest, match="2 m above ground"):
        env.estimate(req(variables=("TMP:3 m above ground",)))


# -- decoding ---------------------------------------------------------------------------------


@needs_eccodes
def test_open_crops_after_decoding_and_carries_the_provenance(tmp_path):
    bucket = es.make_hrrr_bucket(RUN)
    env = make_env(tmp_path, bucket)
    r = req(variables=("cape", "t2m", "u10", "v10"))
    result = env.fetch(r, env.estimate(r).approve())
    ds = env.provider("hrrr").open(result, env)
    assert set(ds.data_vars) == {"cape", "t2m", "u10", "v10"}
    lat, lon = es.lambert_axes()
    inside = (lat >= 35.0) & (lat <= 35.3) & (lon >= -93.8) & (lon <= -93.4)
    jj, ii = np.nonzero(inside)
    assert ds["cape"].shape == (jj.max() - jj.min() + 1, ii.max() - ii.min() + 1)
    # values are the fixture's own function of the position (nothing interpolated)
    j0, i0 = jj.min(), ii.min()
    assert ds["cape"].values[0, 0] == pytest.approx(1000 + 10 * j0 + i0, abs=1e-3)
    assert ds["t2m"].values[0, 0] == pytest.approx(280 + 0.1 * i0, abs=1e-3)
    assert np.all(ds["latitude"].values >= 35.0 - 0.1)
    a = ds.attrs
    assert a["data_class"] == "model" and "HRRR" in a["data_source"]
    assert (
        a["data_run_time"] == "2021-12-11T03:00:00Z"
        and a["data_valid_time"] == "2021-12-11T03:00:00Z"
    )
    assert "3 km" in a["data_resolution"] and "not an observation" in a["data_uncertainty"]
    assert "NOAA" in a["data_attribution"] and a["time_selection"].startswith(
        "run 2021-12-11 03 UTC"
    )
    assert "+10 min" in a["time_selection"]


@needs_eccodes
def test_grid_relative_winds_are_rotated_to_east_and_north(tmp_path):
    bucket = es.make_hrrr_bucket(RUN)  # u10 = 10, v10 = 0 along the grid axes
    env = make_env(tmp_path, bucket)
    r = req(variables=("u10", "v10"))
    ds = env.provider("hrrr").open(env.fetch(r, env.estimate(r).approve()), env)
    g = np.sin(np.radians(38.5)) * np.radians(((ds["longitude"].values + 97.5 + 180) % 360) - 180)
    assert ds["u10"].values == pytest.approx(10 * np.cos(g), abs=1e-4)
    assert ds["v10"].values == pytest.approx(-10 * np.sin(g), abs=1e-4)
    speed = np.hypot(ds["u10"].values, ds["v10"].values)
    assert speed == pytest.approx(10.0, abs=1e-4)  # a rotation keeps the speed


@needs_eccodes
def test_a_single_wind_component_is_flagged_not_silently_used(tmp_path):
    bucket = es.make_hrrr_bucket(RUN)
    env = make_env(tmp_path, bucket)
    r = req(variables=("u10",))
    ds = env.provider("hrrr").open(env.fetch(r, env.estimate(r).approve()), env)
    assert (
        "notes" in ds.attrs["data_parameters"]
        and "partner component" in ds.attrs["data_parameters"]
    )


@needs_eccodes
def test_wildcard_levels_become_a_vertical_axis(tmp_path):
    bucket = es.make_hrrr_bucket(RUN)
    env = make_env(tmp_path, bucket)
    r = Request(
        provider="hrrr",
        product="pressure",
        variables=("TMP:@mb",),
        time_start="2021-12-11T03:00:00Z",
        bbox=(-93.8, 35.0, -93.4, 35.3),
    )
    ds = env.provider("hrrr").open(env.fetch(r, env.estimate(r).approve()), env)
    assert ds["tmp"].dims[0] == "level_tmp"
    assert (
        ds["level_tmp"].values.tolist() == [500.0, 700.0, 850.0]
        and ds["level_tmp"].attrs["units"] == "hPa"
    )
    assert ds["tmp"].values[0, 0, 0] == pytest.approx(260 - 50.0)  # 500 hPa: 260 - (1000-500)/10


def test_open_without_eccodes_says_how_to_get_it(tmp_path, monkeypatch):
    from engine.environment import gribio

    monkeypatch.setitem(__import__("sys").modules, "eccodes", None)
    with pytest.raises(gribio.GribUnavailable, match=r"radarx-desktop-engine\[grib\]"):
        gribio.require_eccodes()

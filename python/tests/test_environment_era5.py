"""The ERA5 provider on a synthetic ARCO-style Zarr store served from memory.

The store has the real layout (Zarr v2, Blosc lz4 chunks of one hour of the whole globe, 37
levels, 0.25 degree grid, hours since 1900) with smooth known values, so a piece decoded by the
provider can be compared with the function that made it. No network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pytest

pytest.importorskip("numcodecs")
from numcodecs import Blosc

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
from engine.environment.providers import era5
from tests.env_synth import Bucket

BASE = "https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3/"
VALID = datetime(2021, 12, 11, 3, tzinfo=UTC)
IDX = era5.hours_since_epoch(VALID)
CODEC = Blosc(cname="lz4", clevel=5, shuffle=1, blocksize=0)
LAT = (90.0 - 0.25 * np.arange(721)).astype("<f4")
LON = (0.25 * np.arange(1440)).astype("<f4")


def field(level=None, offset=0.0):
    """value = offset + 0.01 * level + 0.001 * row + 1e-6 * column (exact enough in float32)."""
    j = (0.001 * np.arange(721, dtype="float64"))[:, None]
    i = (1e-6 * np.arange(1440, dtype="float64"))[None, :]
    if level is None:
        return (offset + j + i).astype("<f4")[None]
    lv = (0.01 * np.array(era5.LEVELS, dtype="float64"))[:, None, None]
    return (offset + lv + j[None] + i[None]).astype("<f4")[None]


def zarray(shape, chunks):
    return json.dumps(
        {
            "chunks": chunks, "compressor": {"blocksize": 0, "clevel": 5, "cname": "lz4",
            "id": "blosc", "shuffle": 1}, "dtype": "<f4", "fill_value": "NaN", "filters": None,
            "order": "C", "shape": shape, "zarr_format": 2,
        }
    ).encode()  # fmt: skip


def add_axes(bucket):
    bucket.put(".zattrs", json.dumps({"valid_time_start": "1940-01-01",
                                      "valid_time_stop_era5t": "2030-01-01"}).encode())  # fmt: skip
    c = 67706
    bucket.put(
        "time/.zarray", json.dumps({"chunks": [c], "dtype": "<i8", "shape": [1323648]}).encode()
    )
    k = IDX // c
    bucket.put(f"time/{k}", bytes(CODEC.encode(np.arange(k * c, (k + 1) * c, dtype="<i8"))))
    bucket.put("latitude/0", bytes(CODEC.encode(LAT)))
    bucket.put("longitude/0", bytes(CODEC.encode(LON)))
    bucket.put("level/0", bytes(CODEC.encode(np.array(era5.LEVELS, dtype="<i8"))))


def add_var(bucket, name, data, units="K", long_name=None):
    ndim = data.ndim
    bucket.put(f"{name}/.zarray", zarray([1323648, *data.shape[1:]], [1, *data.shape[1:]]))
    bucket.put(
        f"{name}/.zattrs", json.dumps({"units": units, "long_name": long_name or name}).encode()
    )
    bucket.put(f"{name}/{'.'.join([str(IDX)] + ['0'] * (ndim - 1))}", bytes(CODEC.encode(data)))


@pytest.fixture(scope="module")
def store():
    b = Bucket(BASE)
    add_axes(b)
    add_var(b, "2m_temperature", field(None, 280.0), "K", "2 metre temperature")
    add_var(b, "10m_u_component_of_wind", field(None, 5.0), "m s**-1")
    add_var(b, "temperature", field(0, 200.0), "K", "Temperature")
    add_var(b, "u_component_of_wind", field(0, 0.0), "m s**-1")
    return b


def make_env(tmp_path, bucket, *, offline=False):
    bucket.log.clear()
    http = HttpClient(opener=bucket.opener, min_interval_s=0, backoff_s=0, sleep=lambda s: None)
    env = Environment(
        EnvSettings(cache_dir=str(tmp_path / "cache"), cache_max_mb=256, offline=offline),
        http=http,
        registry=Registry(),
    )
    env.registry.register(era5.Era5Provider())
    return env


def single(**kw):
    kw.setdefault("time_start", "2021-12-11T02:40:00Z")
    kw.setdefault("bbox", (-92.5, 34.0, -85.0, 39.0))
    kw.setdefault("variables", ("t2m", "u10"))
    return Request(provider="era5", product="single", **kw)


def pressure(**kw):
    kw.setdefault("time_start", "2021-12-11T03:00:00Z")
    kw.setdefault("bbox", (-92.5, 34.0, -85.0, 39.0))
    kw.setdefault("variables", ("t", "u"))
    kw.setdefault("params", {"levels": [850, 500]})
    return Request(provider="era5", product="pressure", **kw)


# -- pure logic -------------------------------------------------------------------------------


def test_rows_columns_and_hours():
    assert era5.row_range((-92.5, 34.0, -85.0, 39.0)) == (204, 224)  # 39 N .. 34 N, 0.25 steps
    assert era5.row_range(None) == (0, 720)
    with pytest.raises(InvalidRequest, match="smaller than one"):
        era5.row_range((0.0, 10.01, 1.0, 10.2))
    cols = era5.columns_for((-92.5, 34.0, -85.0, 39.0))
    lon = ((np.arange(1440) * 0.25 + 180) % 360 - 180)[cols]
    assert lon[0] == -92.5 and lon[-1] == -85.0 and np.all(np.diff(lon) > 0)
    assert era5.hours_since_epoch(datetime(1900, 1, 2, tzinfo=UTC)) == 24
    sel = era5.Era5Selection(datetime(2021, 12, 11, 2, 40, tzinfo=UTC))
    assert sel.valid == VALID and sel.offset_s == 1200 and "+20 min" in sel.describe()


# -- planning, consent, download -----------------------------------------------------------------


def test_estimate_is_exact_small_and_downloads_no_data(tmp_path, store):
    env = make_env(tmp_path, store)
    est = env.estimate(single())
    assert est.size_exact and est.requires_approval and len(est.files) == 2
    whole = sum(
        len(store.files[f"{n}/{IDX}.0.0"]) for n in ("2m_temperature", "10m_u_component_of_wind")
    )
    assert 0 < est.total_bytes < whole / 3  # a few blocks of two 2 MB chunks
    # nothing larger than a header or an axis was transferred while planning
    sizes = [len(store.files[u[len(store.base):]]) if r is None else int(r.split("-")[1]) + 1
             for m, u, r in store.log if m == "GET"]  # fmt: skip
    assert max(sizes) <= 70000
    assert any("+20 min" in n and "not interpolated" in n for n in est.notes)
    assert "era5" in est.provider_id and "storage.googleapis.com" in est.files[0].url


def test_fetch_needs_approval_downloads_exactly_the_estimate_then_works_offline(tmp_path, store):
    env = make_env(tmp_path, store)
    r = single()
    est = env.estimate(r)
    with pytest.raises(ApprovalRequired):
        env.fetch(r)
    got = env.fetch(r, est.approve())
    assert got.bytes_downloaded == est.total_bytes  # the network bytes, exactly
    on_disk = sum(p.stat().st_size for p in got.paths)
    assert 0 < on_disk - est.total_bytes < 400 * len(got.paths)  # plus a small header each
    ranged = [rng for m, _u, rng in store.log if m == "GET" and rng]
    assert len(ranged) >= 2
    off = make_env(tmp_path, store, offline=True)
    again = off.fetch(r)
    assert all(again.from_cache) and not store.log
    with pytest.raises(OfflineError):
        off.fetch(single(variables=("t2m", "sp")))


def test_a_server_that_ignores_ranges_is_stopped(tmp_path, store):
    env = make_env(tmp_path, store)
    r = single()
    est = env.estimate(r)
    store.ignore_range = True
    try:
        with pytest.raises(FetchFailed):
            env.fetch(r, est.approve())
    finally:
        store.ignore_range = False


def test_wrong_product_unknown_variable_time_outside(tmp_path, store):
    env = make_env(tmp_path, store)
    with pytest.raises(InvalidRequest, match="pressure-level variable"):
        env.estimate(single(variables=("t",)))
    with pytest.raises(InvalidRequest, match="single-level variable"):
        env.estimate(pressure(variables=("t2m",)))
    with pytest.raises(InvalidRequest, match="not in the ERA5 store"):
        env.estimate(single(variables=("no_such_variable",)))
    with pytest.raises(InvalidRequest, match="ERA5 has no level 777"):
        env.estimate(pressure(params={"levels": [777]}))
    with pytest.raises(InvalidRequest, match="Name the variables"):
        env.estimate(single(variables=()))
    with pytest.raises(InvalidRequest, match="Give the time"):
        env.estimate(single(time_start=None))
    far = datetime(2031, 1, 1, tzinfo=UTC)
    with pytest.raises(InvalidRequest, match="outside"):
        env.provider("era5")._check_axes(env, era5.Era5Selection(far), False)


# -- reading ----------------------------------------------------------------------------------


def test_open_single_level_matches_the_function_that_made_the_store(tmp_path, store):
    env = make_env(tmp_path, store)
    r = single()
    ds = env.provider("era5").open(env.fetch(r, env.estimate(r).approve()), env)
    t = ds["2m_temperature"]
    assert t.dims == ("latitude", "longitude") and t.attrs["units"] == "K"
    assert ds["10m_u_component_of_wind"].attrs["units"] == "m s-1"  # CF spelling
    assert float(ds.latitude[0]) == 34.0 and float(ds.latitude[-1]) == 39.0  # south to north
    assert float(ds.longitude[0]) == -92.5 and float(ds.longitude[-1]) == -85.0
    j = round((90 - 39.0) / 0.25)  # row of the northernmost latitude
    i = round((-92.5 % 360) / 0.25)
    assert float(t.values[-1, 0]) == pytest.approx(280.0 + 0.001 * j + 1e-6 * i, abs=1e-3)
    assert float(t.values[0, 0]) == pytest.approx(280.0 + 0.001 * (j + 20) + 1e-6 * i, abs=1e-3)
    a = ds.attrs
    assert a["data_class"] == "model" and a["data_valid_time"] == "2021-12-11T03:00:00Z"
    assert "ERA5" in a["data_source"] and "0.25 degree" in a["data_resolution"]
    assert "reanalysis" in a["data_method"] and "Copernicus" in a["data_attribution"]
    assert "not a measurement" in a["data_uncertainty"] and "+20 min" in a["time_selection"]


def test_open_pressure_levels_only_the_levels_asked_for(tmp_path, store):
    env = make_env(tmp_path, store)
    r = pressure()
    est = env.estimate(r)
    whole = len(store.files[f"temperature/{IDX}.0.0.0"]) + len(
        store.files[f"u_component_of_wind/{IDX}.0.0.0"]
    )
    assert est.total_bytes < whole / 2
    ds = env.provider("era5").open(env.fetch(r, est.approve()), env)
    assert ds["temperature"].dims == ("level", "latitude", "longitude")
    assert ds["level"].values.tolist() == [500.0, 850.0]  # ascending, hPa
    j = round((90 - 39.0) / 0.25)
    i = round((-92.5 % 360) / 0.25)
    assert float(ds["temperature"].values[0, -1, 0]) == pytest.approx(
        200 + 5.0 + 0.001 * j + 1e-6 * i, abs=2e-3
    )
    assert float(ds["temperature"].values[1, -1, 0]) == pytest.approx(
        200 + 8.5 + 0.001 * j + 1e-6 * i, abs=2e-3
    )


def test_a_cached_piece_is_independent_of_the_listing(tmp_path, store):
    env = make_env(tmp_path, store)
    r = single()
    got = env.fetch(r, env.estimate(r).approve())
    raw = era5.Era5Provider._rows(got.paths[0])  # decodes from the file alone
    assert raw.shape == (21, 1440)

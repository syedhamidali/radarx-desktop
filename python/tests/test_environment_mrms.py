"""The MRMS provider on an in-memory copy of the bucket layout (S3 listing + gzip GRIB2 files).

The GRIB2 messages are synthetic (ecCodes, a regular 0.01 degree grid, north to south like the real
files); values are a known function of position. No network.
"""

from __future__ import annotations

import gzip
import urllib.parse
from datetime import UTC, datetime
from xml.sax.saxutils import escape

import numpy as np
import pytest

from engine.environment import (
    ApprovalRequired,
    Environment,
    EnvSettings,
    HttpClient,
    InvalidRequest,
    OfflineError,
    Registry,
    Request,
)
from engine.environment.providers import mrms
from tests import env_synth as es
from tests.env_synth import Bucket, _Resp

pytestmark = pytest.mark.skipif(not es.have_eccodes(), reason="ecCodes (extra 'grib') missing")

NY, NX = 60, 80
LAT0, LON0 = 40.0, 270.0  # north-west corner; -90 degrees east


class MrmsBucket(Bucket):
    """Adds the S3 ``list-type=2`` listing to the in-memory file server."""

    def opener(self, req, timeout):
        url = req.full_url
        if "?list-type=2" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            prefix = q["prefix"][0]
            self.log.append(("LIST", url, None))
            rows = "".join(
                f"<Contents><Key>{escape(k)}</Key><Size>{len(v)}</Size></Contents>"
                for k, v in sorted(self.files.items())
                if k.startswith(prefix)
            )
            body = (
                f"<ListBucketResult xmlns='http://s3.amazonaws.com/doc/'>{rows}</ListBucketResult>"
            )
            return _Resp(body.encode(), {"Content-Length": str(len(body.encode()))})
        return super().opener(req, timeout)


def field(offset=0.0):
    jj, ii = np.mgrid[0:NY, 0:NX].astype(float)
    return offset + 0.1 * jj + 0.001 * ii


def put(bucket, folder, when, offset=0.0, name="CAPE", step=0.01):
    run = when.replace(second=0)
    msg = es.message(
        name, field(offset), run=run, grid="regular_ll", lat_first=LAT0, lon_first=LON0, step=step
    )
    key = f"CONUS/{folder}/{when:%Y%m%d}/MRMS_{folder}_{when:%Y%m%d-%H%M%S}.grib2.gz"
    bucket.put(key, gzip.compress(msg))
    return key


@pytest.fixture()
def bucket():
    b = MrmsBucket(mrms.BUCKET_URL + "/")
    t = datetime(2021, 12, 11, 3, 0, 39, tzinfo=UTC)
    for minute, off in ((0, 0.0), (2, 100.0), (4, 200.0)):
        put(b, "MESH_00.50", t.replace(minute=minute, second=39), off)
    put(b, "MergedReflectivityQCComposite_00.50", t, 10.0)
    put(b, "PrecipFlag_00.00", t.replace(second=0), 0.0)
    put(b, "MultiSensor_QPE_01H_Pass2_00.00", t.replace(second=0), 5.0)
    put(b, "Model_0degC_Height_00.50", t.replace(second=0), 3000.0)
    put(b, "RotationTrack30min_00.50", t.replace(second=0), 7.0, step=0.005)  # a finer grid
    for h in (0.5, 5.0, 10.0):
        put(b, f"MergedReflectivityQC_{h:05.2f}", t, h)
    return b


def make_env(tmp_path, bucket, *, offline=False):
    http = HttpClient(opener=bucket.opener, min_interval_s=0, backoff_s=0, sleep=lambda s: None)
    env = Environment(
        EnvSettings(cache_dir=str(tmp_path / "cache"), cache_max_mb=64, offline=offline),
        http=http,
        registry=Registry(),
    )
    env.registry.register(mrms.MrmsProvider())
    return env


def req(*variables, **kw):
    kw.setdefault("time_start", "2021-12-11T03:02:30Z")
    kw.setdefault("bbox", (-89.9, 39.5, -89.5, 39.9))
    return Request(provider="mrms", variables=tuple(variables), **kw)


# -- planning ----------------------------------------------------------------------------------


def test_nearest_file_is_chosen_named_and_sized_exactly(tmp_path, bucket):
    env = make_env(tmp_path, bucket)
    est = env.estimate(req("mesh"))
    (f,) = est.files
    assert f.name.endswith("20211211-030239.grib2.gz")  # 03:02:39 is 9 s from 03:02:30
    assert f.size == len(bucket.files[f.key]) and est.size_exact and est.requires_approval
    assert any("9" in n and "nearest file" in n and "not interpolated" in n for n in est.notes)
    assert all(m != "GET" for m, _u, _r in bucket.log)  # only the listing was read
    assert "noaa-mrms-pds" in f.url and "area is cropped after decoding" in " ".join(est.notes)


def test_a_time_series_takes_every_file_in_the_window(tmp_path, bucket):
    env = make_env(tmp_path, bucket)
    est = env.estimate(
        req("mesh", time_start="2021-12-11T03:00:00Z", time_end="2021-12-11T03:05:00Z")
    )
    assert len(est.files) == 3 and any("A series: 3 file(s)" in n for n in est.notes)


def test_errors_are_clear(tmp_path, bucket):
    env = make_env(tmp_path, bucket)
    with pytest.raises(InvalidRequest, match="Name the products"):
        env.estimate(req())
    with pytest.raises(InvalidRequest, match=r"no 'NoSuchProduct_00\.50' files"):
        env.estimate(req("NoSuchProduct_00.50"))
    with pytest.raises(InvalidRequest, match="not a product name"):
        env.estimate(req("../etc"))
    with pytest.raises(InvalidRequest, match="Give the time"):
        env.estimate(req("mesh", time_start=None))
    with pytest.raises(InvalidRequest, match=r"no 'MESH_00\.50' files near 2021-12-11 05:00"):
        env.estimate(req("mesh", time_start="2021-12-11T05:00:00Z"))
    with pytest.raises(InvalidRequest, match="needs params heights_km"):
        env.estimate(req("reflectivity_3d"))
    with pytest.raises(InvalidRequest, match=r"no reflectivity at 5\.1 km"):
        env.estimate(req("reflectivity_3d", params={"heights_km": [5.1]}))


# -- consent, download, cache, reading -------------------------------------------------------------


def test_download_reads_open_and_labels(tmp_path, bucket):
    env = make_env(tmp_path, bucket)
    r = req("mesh", "reflectivity_composite")
    est = env.estimate(r)
    with pytest.raises(ApprovalRequired):
        env.fetch(r)
    got = env.fetch(r, est.approve())
    assert got.bytes_downloaded == est.total_bytes == sum(p.stat().st_size for p in got.paths)
    ds = env.provider("mrms").open(got, env)
    assert set(ds.data_vars) == {"mesh", "reflectivity_composite"}
    m = ds["mesh"]
    assert m.dims == ("latitude", "longitude") and m.attrs["mrms_product"] == "MESH_00.50"
    # the box -89.9..-89.5, 39.5..39.9 of a grid starting at 40 N, -90 E in 0.01 steps
    assert float(ds.latitude[0]) == pytest.approx(39.5) and float(ds.latitude[-1]) == pytest.approx(
        39.9
    )
    assert float(ds.longitude[0]) == pytest.approx(-89.9) and float(
        ds.longitude[-1]
    ) == pytest.approx(-89.5)
    # nothing interpolated: the value at (39.9 N, -89.9 E) is the file's cell (row 10, column 10)
    expected = field(100.0)[10, 10]
    assert float(m.values[-1, 0]) == pytest.approx(expected, abs=1e-3)
    a = ds.attrs
    assert a["data_class"] == "retrieved" and "MRMS" in a["data_source"]
    # composite at 03:00:39, MESH at 03:02:39: the earlier one starts the window
    assert a["data_valid_time"] == "2021-12-11T03:00:39Z" and "1 km" in a["data_resolution"]
    assert "radars" in a["data_method"] and "NOAA" in a["data_attribution"]
    assert "nearest file" in a["data_parameters"] and "030239" not in a["time_selection"]
    assert "MESH_00.50" in a["time_selection"] and "+9 s" in a["time_selection"]
    again = env.fetch(r)  # cached
    assert all(again.from_cache)


def test_offline_uses_the_cache_only(tmp_path, bucket):
    env = make_env(tmp_path, bucket)
    r = req("mesh")
    env.fetch(r, env.estimate(r).approve())
    off = make_env(tmp_path, bucket, offline=True)
    n = len(bucket.log)
    assert all(off.fetch(r).from_cache) and len(bucket.log) == n
    with pytest.raises(OfflineError):
        off.fetch(req("reflectivity_composite"))


def test_classes_say_what_each_product_is(tmp_path, bucket):
    env = make_env(tmp_path, bucket)
    for name, kind, word in (
        ("qpe_gauge_1h", "retrieved", "gauges"),
        ("precip_type", "retrieved", "model temperature"),
        ("freezing_level", "model", "numerical-model"),
    ):
        r = req(name)
        ds = env.provider("mrms").open(env.fetch(r, env.estimate(r).approve()), env)
        assert ds.attrs["data_class"] == kind, name
        assert word in ds.attrs["data_method"], name


def test_3d_reflectivity_stacks_the_heights(tmp_path, bucket):
    env = make_env(tmp_path, bucket)
    r = req("reflectivity_3d", params={"heights_km": [10, 0.5, 5]})
    est = env.estimate(r)
    assert len(est.files) == 3
    ds = env.provider("mrms").open(env.fetch(r, est.approve()), env)
    z = ds["reflectivity_3d"]
    assert z.dims == ("height_km", "latitude", "longitude")
    assert ds["height_km"].values.tolist() == [0.5, 5.0, 10.0]
    assert float(z.values[1, -1, 0]) == pytest.approx(field(5.0)[10, 10], abs=1e-3)
    assert ds["height_km"].attrs["units"] == "km"


def test_products_on_another_grid_keep_their_own_coordinates(tmp_path, bucket):
    env = make_env(tmp_path, bucket)
    r = req("mesh", "rotation_30min", bbox=(-89.95, 39.8, -89.7, 39.95))
    ds = env.provider("mrms").open(env.fetch(r, env.estimate(r).approve()), env)
    assert ds["mesh"].dims == ("latitude", "longitude")
    assert ds["rotation_30min"].dims == ("latitude_rotation_30min", "longitude_rotation_30min")
    assert ds["mesh"].attrs["grid_spacing_deg"] == pytest.approx(0.01)
    assert ds["rotation_30min"].attrs["grid_spacing_deg"] == pytest.approx(0.005)
    assert ds["rotation_30min"].shape[0] > ds["mesh"].shape[0]  # twice the cells in the same box
    # each field is cropped on its own grid: the north-west cell of the box in the 0.005 file
    lat = float(ds["latitude_rotation_30min"][-1])
    row = round((LAT0 - lat) / 0.005)
    col = round((float(ds["longitude_rotation_30min"][0]) - (LON0 - 360.0)) / 0.005)
    assert float(ds["rotation_30min"].values[-1, 0]) == pytest.approx(
        field(7.0)[row, col], abs=1e-3
    )
    assert "special_values" in ds.attrs


def test_a_series_has_a_time_axis(tmp_path, bucket):
    env = make_env(tmp_path, bucket)
    r = req("mesh", time_start="2021-12-11T03:00:00Z", time_end="2021-12-11T03:05:00Z")
    ds = env.provider("mrms").open(env.fetch(r, env.estimate(r).approve()), env)
    assert ds["mesh"].dims == ("time_mesh", "latitude", "longitude")
    assert ds["time_mesh"].size == 3 and ds["time_mesh"].values[0] < ds["time_mesh"].values[-1]
    assert float(ds["mesh"].values[2, -1, 0]) == pytest.approx(field(200.0)[10, 10], abs=1e-3)

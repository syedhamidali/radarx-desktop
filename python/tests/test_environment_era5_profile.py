"""ERA5 vertical profile: the analytic Weisman-Klemp sounding is written into a synthetic ARCO-style
store (humidity as specific humidity, geopotential in m2 s-2, like ERA5), read back as a column
through byte-range pieces and analysed with the existing thermodynamics.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("numcodecs")
pytest.importorskip("radarx")

from engine import sounding
from engine.environment import Request
from engine.environment.providers import era5
from tests.env_synth import Bucket
from tests.test_environment_era5 import BASE, IDX, add_axes, add_var, make_env

RE = 6371008.8
G0 = 9.80665


def column(wk):
    ds = wk.profile
    p, z = ds["pressure"].values, ds["height"].values
    t, td = ds["temperature"].values, ds["dewpoint"].values
    u, v = ds["u"].values, ds["v"].values
    lp = -np.log(p)
    lv = np.array(era5.LEVELS, dtype=float) * 100.0

    def at(arr):
        return np.interp(-np.log(lv), lp, arr)

    from radarx.io import sounding as rs

    q = np.asarray(rs.specific_humidity_from_dewpoint(at(td), lv).values)
    gpm = RE * at(z) / (RE + at(z))
    return {
        "geopotential": G0 * gpm, "temperature": at(t), "u_component_of_wind": at(u),
        "v_component_of_wind": at(v), "specific_humidity": q,
    }, (p[0], z[0], t[0], td[0], u[0], v[0])  # fmt: skip


def full(per_level):
    return np.ascontiguousarray(
        np.broadcast_to(per_level.astype("<f4")[:, None, None], (37, 721, 1440))
    )[None]


@pytest.fixture()
def wk_store():
    wk = sounding.weisman_klemp()
    cols, (p0, z0, t0, td0, u0, v0) = column(wk)
    b = Bucket(BASE)
    add_axes(b)
    for name, vals in cols.items():
        add_var(b, name, full(vals), "units")
    ones = np.ones((1, 721, 1440), dtype="<f4")
    for name, val in {
        "surface_pressure": p0, "geopotential_at_surface": G0 * RE * z0 / (RE + z0),
        "2m_temperature": t0, "2m_dewpoint_temperature": td0,
        "10m_u_component_of_wind": u0, "10m_v_component_of_wind": v0,
    }.items():  # fmt: skip
        add_var(b, name, ones * np.float32(val), "units")
    return wk, b


def profile_request(**params):
    return Request(
        provider="era5",
        product="profile",
        time_start="2021-12-11T03:00:00Z",
        params={"lat": 36.74, "lon": -88.64, **params},
    )


def test_profile_cape_matches_the_source_sounding(tmp_path, wk_store):
    wk, bucket = wk_store
    ref = sounding.diagnose(wk)["parcels"]["surface-based"]["cape"]
    env = make_env(tmp_path, bucket)
    r = profile_request()
    est = env.estimate(r)
    assert len(est.files) == 6 + 5 * 19 and est.size_exact
    whole = sum(len(bucket.files[f"{n}/{IDX}.0.0.0"]) for n in
                ("geopotential", "temperature", "u_component_of_wind", "v_component_of_wind",
                 "specific_humidity"))  # fmt: skip
    assert (
        est.total_bytes < 0.5 * whole
    )  # the pieces, not the chunks (the synthetic ones compress hard)
    result = env.fetch(r, est.approve())
    snd = env.provider("era5").profile_at(result, 36.74, -88.64, env)
    assert snd.data_class == "model" and snd.fmt == "era5_zarr"
    got = sounding.diagnose(snd)["parcels"]["surface-based"]["cape"]
    assert got == pytest.approx(ref, rel=0.15)
    pv = snd.to_dict()["provenance"]
    assert pv["kind"] == "model" and pv["valid_time"] == "2021-12-11T03:00:00Z"
    assert "reanalysis" in pv["method"] and "Copernicus" in pv["attribution"]
    assert "grid cell" in " ".join(snd.notes)
    assert snd.profile["pressure"].values[0] == pytest.approx(
        float(wk.profile["pressure"].values[0])
    )


def test_profile_for_another_point_is_refused_not_misused(tmp_path, wk_store):
    from engine.environment import InvalidRequest

    _wk, bucket = wk_store
    env = make_env(tmp_path, bucket)
    r = profile_request(levels=[850, 500])
    result = env.fetch(r, env.estimate(r).approve())
    with pytest.raises(InvalidRequest, match=r"fetched for 36\.74"):
        env.provider("era5").profile_at(result, 40.0, -100.0, env)


def test_profile_needs_a_point(tmp_path, wk_store):
    from engine.environment import InvalidRequest

    _wk, bucket = wk_store
    env = make_env(tmp_path, bucket)
    with pytest.raises(InvalidRequest, match="params lat and lon"):
        env.estimate(Request(provider="era5", product="profile", time_start="2021-12-11T03:00:00Z"))

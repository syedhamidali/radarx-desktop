"""HRRR vertical profile: the column at a point becomes a Sounding the existing thermodynamics
reads (CAPE, shear, helicity), checked against the analytic Weisman-Klemp sounding.

No network: synthetic GRIB2 files hold the analytic profile at every grid cell.
"""

from __future__ import annotations

import numpy as np
import pytest

from engine.environment import Environment, EnvSettings, HttpClient, Registry, Request
from engine.environment.providers import hrrr
from tests import env_synth as es

needs_eccodes = pytest.mark.skipif(not es.have_eccodes(), reason="ecCodes (extra 'grib') missing")
RUN = es.run_time(3)
RE = 6371008.8


def make_env(tmp_path, bucket):
    http = HttpClient(opener=bucket.opener, min_interval_s=0, backoff_s=0, sleep=lambda s: None)
    env = Environment(
        EnvSettings(cache_dir=str(tmp_path / "cache"), cache_max_mb=64),
        http=http,
        registry=Registry(),
    )
    env.registry.register(hrrr.HrrrProvider())
    return env


def gpm(z):
    """Geopotential metres of a geometric height (the inverse of what the engine applies)."""
    return RE * z / (RE + z)


def column_bucket(snd, *, p_sfc=None, z_sfc=0.0, junk_levels=()):
    """A bucket whose surface and pressure files hold *snd* (a sounding) at every grid cell."""
    ds = snd.profile
    p, z = np.asarray(ds["pressure"].values), np.asarray(ds["height"].values)
    t, td = np.asarray(ds["temperature"].values), np.asarray(ds["dewpoint"].values)
    u, v = np.asarray(ds["u"].values), np.asarray(ds["v"].values)
    lp = -np.log(p)

    def at(arr, pres):
        return float(np.interp(-np.log(pres), lp, arr))

    ones = np.ones((es.LAMBERT["ny"], es.LAMBERT["nx"]))
    kw = {"run": RUN, "fxx": 0}
    p0 = float(p[0]) if p_sfc is None else p_sfc
    h2 = {"kind": "height", "level": 2}
    h10 = {"kind": "height", "level": 10}
    sfc = [
        ("PRES", "surface", "anl", es.message("PRES", p0 * ones, **kw)),
        ("HGT", "surface", "anl", es.message("HGT", gpm(z_sfc) * ones, **kw)),
        ("TMP", "2 m above ground", "anl", es.message("TMP", t[0] * ones, **h2, **kw)),
        ("DPT", "2 m above ground", "anl", es.message("DPT", td[0] * ones, **h2, **kw)),
        ("UGRD", "10 m above ground", "anl", es.message("UGRD", u[0] * ones, **h10, **kw)),
        ("VGRD", "10 m above ground", "anl", es.message("VGRD", v[0] * ones, **h10, **kw)),
    ]
    prs = []
    for lv in range(1000, 99, -50):
        pa = lv * 100.0
        if lv in junk_levels:  # below-ground levels hold extrapolated nonsense
            vals = {"HGT": -500.0, "TMP": 400.0, "DPT": 399.0, "UGRD": 99.0, "VGRD": 99.0}
        else:
            vals = {
                "HGT": gpm(at(z, pa)),
                "TMP": at(t, pa),
                "DPT": at(td, pa),
                "UGRD": at(u, pa),
                "VGRD": at(v, pa),
            }
        for name in hrrr.PROFILE_LEVEL_VARS:
            msg = es.message(name, vals[name] * ones, kind="isobaric", level=lv, **kw)
            prs.append((name, f"{lv} mb", "anl", msg))
    bucket = es.Bucket("https://noaa-hrrr-bdp-pds.s3.amazonaws.com/")
    day = "hrrr.20211211/conus/hrrr.t03z"
    for code, msgs in (("wrfsfc", sfc), ("wrfprs", prs)):
        body, idx = es.hrrr_file(msgs, RUN)
        bucket.put(f"{day}.{code}f00.grib2", body)
        bucket.put(f"{day}.{code}f00.grib2.idx", idx.encode())
    return bucket


def profile_request():
    return Request(provider="hrrr", product="profile", time_start="2021-12-11T03:00:00Z")


@needs_eccodes
def test_profile_cape_matches_the_source_sounding_through_the_existing_thermodynamics(tmp_path):
    pytest.importorskip("radarx")
    from engine import sounding

    wk = sounding.weisman_klemp()
    ref = sounding.diagnose(wk)["parcels"]["surface-based"]["cape"]
    assert ref and ref > 1500  # the analytic case is a strongly unstable day
    env = make_env(tmp_path, column_bucket(wk))
    r = profile_request()
    est = env.estimate(r)
    assert len(est.files) == 6 + 5 * 19  # surface values + 5 variables on 19 levels (1000..100 hPa)
    result = env.fetch(r, est.approve())
    snd = env.provider("hrrr").profile_at(result, 35.2, -93.6, env)
    assert snd.data_class == "model" and snd.fmt == "hrrr_grib2"
    got = sounding.diagnose(snd)["parcels"]["surface-based"]["cape"]
    # a 50 hPa column of the analytic sounding keeps the instability to within 15 %
    assert got == pytest.approx(ref, rel=0.15)
    pv = snd.to_dict()["provenance"]
    assert pv["kind"] == "model" and pv["run_time"] == "2021-12-11T03:00:00Z"
    assert "grid cell" in " ".join(snd.notes)
    assert snd.profile["pressure"].values[0] == pytest.approx(
        float(wk.profile["pressure"].values[0])
    )


@needs_eccodes
def test_profile_drops_levels_below_the_model_ground(tmp_path):
    from engine import sounding

    wk = sounding.weisman_klemp()
    # terrain at 96000 Pa: the 1000 hPa level is below ground and holds nonsense
    bucket = column_bucket(wk, p_sfc=96000.0, z_sfc=400.0, junk_levels=(1000,))
    env = make_env(tmp_path, bucket)
    r = profile_request()
    snd = env.provider("hrrr").profile_at(env.fetch(r, env.estimate(r).approve()), 35.2, -93.6, env)
    p = snd.profile["pressure"].values
    t = snd.profile["temperature"].values
    assert p[0] == pytest.approx(96000.0) and p.max() == pytest.approx(96000.0)
    assert t.max() < 350.0  # the 400 K nonsense is not in the column
    assert "1 pressure level(s) at or below the model ground were dropped" in " ".join(snd.notes)

"""Real NEXRAD volumes (Mayfield, 11 Dec 2021: KHPX / KNQA / KPAH / KVWX): sanity, not truth.

There is no ground truth for a real storm here (no gauges are fetched), so these tests check what
must hold: physical ranges, consistency between methods and between advected and linear time
stepping, the storm motion, the composite of several radars. Skipped when the data are absent
(``$XRADAR_SEVERE_CASES`` or ``~/Downloads/SEVERE_CASES_TEST``).
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("radarx")

from engine import api
from engine.hydro import accumulate as acc
from engine.hydro import basins as hb
from engine.hydro import gauges as hg

ROOT = Path(os.environ.get("XRADAR_SEVERE_CASES", "~/Downloads/SEVERE_CASES_TEST")).expanduser()
MAYFIELD = ROOT / "mayfield"

pytestmark = pytest.mark.skipif(
    not (MAYFIELD / "KHPX").is_dir(), reason="Mayfield NEXRAD volumes are not available"
)

EXTENT = (-92.9, 33.2, -84.6, 40.4)


@pytest.fixture(scope="module")
def khpx():
    files = sorted(glob.glob(str(MAYFIELD / "KHPX" / "KHPX*_V06")))
    assert len(files) >= 3
    vols = [api.open(f) for f in files]
    yield [v._reader for v in vols]
    for v in vols:
        v.close()


@pytest.fixture(scope="module")
def series(khpx):
    return acc.accumulate(khpx, {}, resolution_m=3000.0, extent=EXTENT)


def _volume(r):
    return float(np.nansum(r.accumulation * r.grid.cell_areas_km2()))


def test_accumulation_is_physical(series):
    r = series
    assert r.provenance()["type"] == "RETRIEVED"
    assert 15 * 60 < r.duration_s < 20 * 60  # four scans over about 17 minutes
    a = r.accumulation[np.isfinite(r.accumulation)]
    assert a.min() >= 0
    assert 5.0 < a.max() < 40.0  # a supercell core: tens of mm/h for a quarter of an hour
    assert 50.0 < np.nanmax(r.rate_max) < 262.0
    assert np.nanmean(r.valid_fraction[np.isfinite(r.accumulation)]) > 0.9
    rel = r.stats()["uncertainty_median_rel"]
    assert 0.1 < rel < 0.7


def test_storm_motion_is_to_the_north_east(series):
    u, v = series.radars[0]["motion_mean_ms"]
    # the line moved from the south-west at about 25-30 m/s
    assert 10 < u < 35 and 8 < v < 30


def test_advection_conserves_the_rain_and_follows_the_pattern(khpx, series):
    lin = acc.accumulate(khpx, {}, resolution_m=3000.0, extent=EXTENT, advect=False)
    assert _volume(series) == pytest.approx(_volume(lin), rel=0.12)
    a = np.nan_to_num(series.accumulation).ravel()
    b = np.nan_to_num(lin.accumulation).ravel()
    assert np.corrcoef(a, b)[0, 1] > 0.9


def test_methods_agree_to_the_stated_uncertainty(khpx, series):
    z = acc.accumulate(khpx, {"method": "z"}, resolution_m=3000.0, extent=EXTENT)
    trop = acc.accumulate(
        khpx, {"method": "z", "relation": "tropical"}, resolution_m=3000.0, extent=EXTENT
    )
    # hybrid (polarimetric) vs R(Z): same order, the hybrid not above the capped R(Z) by much
    assert 0.55 < _volume(series) / _volume(z) < 1.1
    # Z = 250 R^1.2 gives more rain than Z = 300 R^1.4 at these reflectivities (35-50 dBZ)
    wet = np.isfinite(z.accumulation) & (z.accumulation > 1.0)
    ratio = np.median(trop.accumulation[wet] / z.accumulation[wet])
    assert 1.4 < ratio < 2.6


def test_one_volume_per_radar_makes_a_network_composite(tmp_path):
    files = []
    for rr in ("KHPX", "KNQA", "KPAH", "KVWX"):
        found = sorted(glob.glob(str(MAYFIELD / rr / f"{rr}*_V06")))
        if not found:
            pytest.skip(f"no {rr} volume")
        files.append(found[1])
    best = api.qpe(files, composite="best", resolution_m=3000.0, extent=EXTENT)
    wtd = api.qpe(files, composite="weighted", resolution_m=3000.0, extent=EXTENT)
    assert len(best.radars) == 4 and int(best.n_radars.max()) == 4
    assert np.isfinite(best.rate_last).sum() == np.isfinite(wtd.rate_last).sum()
    assert np.nanmax(wtd.rate_last) == pytest.approx(np.nanmax(best.rate_last), rel=0.15)
    assert any("apart" in n for n in best.notes)  # scan times differ by minutes: said, not hidden
    # quality: close to a radar is better than far from every radar
    near = best.range_km < 40
    far = best.range_km > 150
    assert np.nanmean(best.quality[near]) > 2 * np.nanmean(best.quality[far])
    out = best.save(tmp_path / "net.nc")
    assert out[0].stat().st_size > 10_000


def test_basin_and_gauge_chain_on_real_data(khpx):
    basin = hb.BasinSet.from_polygon(
        [(-87.9, 36.4), (-87.2, 36.4), (-87.2, 37.0), (-87.9, 37.0)], "track"
    )
    r = acc.accumulate(khpx, {}, resolution_m=2000.0, extent=EXTENT, basins=basin)
    s = r.basin_series.summary()[0]
    assert s["coverage"] == pytest.approx(1.0, abs=0.02)
    assert abs(s["area_km2"] / r.basin_series.weights[0].geodesic_area_km2 - 1) < 2e-3
    assert s["accumulation_mean_mm"] > 0.05
    # synthetic gauges: the radar times 1.3 at wet cells; the mean-field bias must recover 1.3
    acc_f = r.accumulation
    wet = np.argwhere(np.isfinite(acc_f) & (acc_f > 2.0) & (r.range_km > 20) & (r.range_km < 150))
    rng = np.random.default_rng(5)
    pick = wet[rng.choice(len(wet), 14, replace=False)]
    import pandas as pd
    import pyproj

    tr = pyproj.Transformer.from_crs(r.grid.proj4(), "EPSG:4326", always_xy=True)
    rows = []
    for i, (j, k) in enumerate(pick):
        lon, lat = tr.transform(r.grid.x[k], r.grid.y[j])
        rows.append(
            {
                "station": f"S{i}",
                "lat": lat,
                "lon": lon,
                "name": "s",
                "elevation_m": 0.0,
                "depth": float(acc_f[j, k]) * 1.3,
            }
        )
    st = pd.DataFrame(rows)
    rec = pd.DataFrame(
        {
            "station": st["station"],
            "time_end": pd.Timestamp(np.datetime64(r.end, "ns")),
            "interval_s": r.duration_s,
            "depth_mm": st["depth"],
            "flag": "",
        }
    )
    gs = hg.GaugeSet(st.drop(columns="depth"), rec, "synthetic")
    rep = hg.adjust(r, gs, method="mfb", min_amount=1.0, tolerance_s=10)
    assert r.adjusted["parameters"]["mean_field_bias"] == pytest.approx(1.3, rel=0.12)
    assert rep["validation_before"]["overall"]["relative_bias"] < -0.15
    assert abs(rep["validation_after_in_sample"]["overall"]["relative_bias"]) < 0.02

"""Tests of the PERiLS movie bundle builders (tools/perils_movie) on tiny synthetic inputs.

No real data are needed: every expected value comes from an analytic case (a solid-body rotation, a uniform
wind, an exponential drop size distribution with N(D) = N0 exp(-L D) whose Dm is 4 / L and Nw is N0, a shifted
blob, hand-computed weights), not from running the code under test.
"""
# ruff: noqa: N806, E501, RUF012, E731  (radar maths: X, Y, R, T, P are the usual capitals; long assert lines)

from __future__ import annotations

import gzip
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "perils_movie"
sys.path.insert(0, str(TOOLS))

import bundle
import context
import events
import hailconv
import merge
import pgrid
import rotation
import stations
import thermo
import tornadoes
import tracks
import volumes
import winds

UTC = timezone.utc
T0 = datetime(2022, 3, 31, 1, 4, tzinfo=UTC)
HAS_RADARX = True
try:
    import radarx  # noqa: F401
except Exception:  # pragma: no cover
    HAS_RADARX = False
needs_radarx = pytest.mark.skipif(not HAS_RADARX, reason="radarx not installed")


# ---------------------------------------------------------------------------
# grids: heights, names, time steps
# ---------------------------------------------------------------------------


class TestGrid:
    def test_shift_to_asl_linear_field(self):
        # a field equal to its own height above the radar site: after the shift it equals height ASL - site altitude
        nz = 41
        z_rel = np.arange(nz) * 250.0
        a = np.broadcast_to(z_rel[:, None, None], (nz, 2, 2)).astype(np.float32).copy()
        out = pgrid.shift_to_asl(a, 330.0)  # NOXP: site at 330 m
        z_asl = np.arange(nz) * 250.0
        want = z_asl - 330.0
        ok = z_asl >= 330.0 + 0.0
        # interior levels (not the first one under the site, not the top edge) follow the line exactly
        inner = ok & (z_asl <= 9700)
        np.testing.assert_allclose(out[inner, 0, 0], want[inner], atol=1e-3)
        assert np.isnan(
            out[z_asl < 330.0 - 250.0, 0, 0]
        ).all()  # more than a level under the site: no data

    def test_shift_keeps_nan_gaps(self):
        a = np.full((6, 1, 1), np.nan, np.float32)
        a[2, 0, 0] = 10.0
        out = pgrid.shift_to_asl(a, 0.0)
        assert out[2, 0, 0] == 10.0 and np.isnan(out[3, 0, 0])

    def test_time_from_names(self):
        p = pgrid.parse_time
        assert p("KGWX20220331_011153_V06.nc") == datetime(2022, 3, 31, 1, 11, 53, tzinfo=UTC)
        assert p("cfrad.20220331_011013.013_to_20220331_011143.1_COW1high_SUR.nc") == datetime(
            2022, 3, 31, 1, 10, 13, tzinfo=UTC
        )
        assert p("RAW_NA_000_100_20220331011003.nc") == datetime(2022, 3, 31, 1, 10, 3, tzinfo=UTC)
        assert p("NOX220331011202.nc") == datetime(2022, 3, 31, 1, 12, 2, tzinfo=UTC)
        assert p("readme.txt") is None

    def test_zdr_offset_from_rain(self):
        nz, ny, nx = 12, 40, 40
        z = np.arange(nz) * 250.0
        f = {
            "dbz": np.full((nz, ny, nx), 32.0, np.float32),
            "zdr": np.full((nz, ny, nx), 3.55, np.float32),
            "rho": np.full((nz, ny, nx), 0.99, np.float32),
        }
        off, n = pgrid.estimate_zdr_offset(f, z)
        assert n > 3000
        assert off == pytest.approx(3.55 - pgrid.ZDR_RAIN_EXPECTED_DB, abs=1e-4)
        f["dbz"][:] = 10.0  # no rain-like echo: no estimate
        assert pgrid.estimate_zdr_offset(f, z)[0] is None

    def test_qc_off_and_on_roundtrip(self):
        q = pgrid.QCConfig()
        assert q.roi_max_m and q.rho_min and q.min_dbz == 0.0
        off = pgrid.QCConfig.off()
        assert off.roi_max_m is None and off.rho_min is None and off.min_dbz is None
        assert q.as_dict()["ref_range"] == [-30.0, 80.0]


class TestSteps:
    def _idx(self):
        def ref(r, h, m, s, dur=120):
            t = datetime(2022, 3, 31, h, m, s, tzinfo=UTC)
            return merge.VolumeRef(r, Path(f"{r}.nc"), t, t + timedelta(seconds=dur))

        return {
            "A": [ref("A", 1, 4, 0), ref("A", 1, 6, 0), ref("A", 1, 8, 0)],
            "B": [ref("B", 1, 3, 0, 360), ref("B", 1, 9, 0, 360)],  # 6 min cycle
        }

    def test_window_times_include_end(self):
        t = merge.window_times(T0, T0 + timedelta(seconds=960), 60.0)
        assert len(t) == 17 and t[-1] == T0 + timedelta(seconds=960)

    def test_plan_offsets_and_tolerance(self):
        plans = merge.plan_steps(self._idx(), T0, T0 + timedelta(minutes=4), 60.0, tol_s=190.0)
        # step 0 = 01:04:00; A volume mid-times 01:05:00, 01:07:00, 01:09:00; B mid 01:06:00, 01:12:00
        p0 = plans[0]
        assert p0.offsets_s["A"] == pytest.approx(60.0)  # 01:05:00 - 01:04:00
        assert p0.offsets_s["B"] == pytest.approx(120.0)  # 01:06:00 - 01:04:00
        # step at 01:08:00: B nearest is 01:06:00 (offset -120) and within 190 s; with tol 100 s it is dropped
        p4 = plans[4]
        assert p4.offsets_s["B"] == pytest.approx(-120.0)
        tight = merge.plan_steps(self._idx(), T0, T0 + timedelta(minutes=4), 60.0, tol_s=100.0)
        assert "B" not in tight[4].offsets_s and "A" in tight[4].offsets_s


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


def _grid(name, dbz, vel=None, zdr=None, rho=None, roi=1000.0, shape=(1, 1, 3)):
    f = {"dbz": np.full(shape, dbz, np.float32), "roi": np.full(shape, roi, np.float32)}
    if vel is not None:
        f["vel"] = np.full(shape, vel, np.float32)
    if zdr is not None:
        f["zdr"] = np.full(shape, zdr, np.float32)
        f["rho"] = np.full(shape, 0.99 if rho is None else rho, np.float32)
    t = T0
    return pgrid.RadarGrid(name, Path(f"{name}.nc"), t, t, 33.0, -88.0, 100.0, f)


class TestMerge:
    def test_linear_z_weighted_mean(self):
        # S-band radar (weight 1/ROI^2 with ROI 1 km) at 30 dBZ and X-band radar (0.5 / ROI^2 with ROI 2 km) at 40 dBZ
        m = merge.Merger((1, 1, 3), ["KGWX", "NOXP"])
        m.add("KGWX", _grid("KGWX", 30.0, roi=1000.0))
        m.add("NOXP", _grid("NOXP", 40.0, roi=2000.0))
        r = m.result(min_dbz=None)
        w1, w2 = 1.0 / 1.0**2, 0.5 / 2.0**2
        z = (w1 * 10**3.0 + w2 * 10**4.0) / (w1 + w2)
        assert r["dbz"][0, 0, 0] == pytest.approx(10 * math.log10(z), abs=1e-4)
        assert (r["nradars"] == 2).all()

    def test_velocity_comes_from_the_best_radar(self):
        m = merge.Merger((1, 1, 3), ["A", "B"])
        m.add("A", _grid("A", 30.0, vel=5.0, roi=3000.0))
        m.add("B", _grid("B", 30.0, vel=-12.0, roi=800.0))
        r = m.result(min_dbz=None)
        assert (r["vel"] == -12.0).all() and (
            r["vel_src"] == 2
        ).all()  # B has the smaller ROI, not a mean

    def test_dual_pol_never_mixed_between_radars(self):
        m = merge.Merger((1, 1, 3), ["A", "B"])
        m.add("A", _grid("A", 35.0, zdr=0.7, roi=900.0))
        m.add("B", _grid("B", 35.0, zdr=3.0, roi=1500.0))
        r = m.result(min_dbz=None)
        assert (r["zdr"] == np.float32(0.7)).all() and (r["pol_src"] == 1).all()

    def test_no_data_stays_nan_and_min_dbz(self):
        m = merge.Merger((1, 1, 3), ["A"])
        g = _grid("A", 30.0, vel=3.0)
        g.fields["dbz"][0, 0, 1] = np.nan
        g.fields["dbz"][0, 0, 2] = -5.0
        m.add("A", g)
        r = m.result(min_dbz=0.0)
        assert np.isnan(r["dbz"][0, 0, 1]) and np.isnan(r["dbz"][0, 0, 2])
        assert (
            np.isnan(r["vel"][0, 0, 1]) and r["nradars"][0, 0, 2] == 0
        )  # no echo: no velocity, no count


# ---------------------------------------------------------------------------
# rotation
# ---------------------------------------------------------------------------


def _solid_body(omega=0.01, radar=(-10.0, 0.0)):
    x = np.arange(-20, 20.01, 0.5)
    y = x.copy()
    X, Y = np.meshgrid(x * 1000, y * 1000)
    u, w = -omega * Y, omega * X  # counter-clockwise (cyclonic) about the origin
    rx, ry = radar[0] * 1000, radar[1] * 1000
    R = np.hypot(X - rx, Y - ry)
    vr = (u * (X - rx) + w * (Y - ry)) / R
    return x, y, X, Y, vr.astype(np.float32)


class TestRotation:
    def test_plane_gradient_exact_for_linear_field(self):
        _x, _y, X, Y, _ = _solid_body()
        v = (0.003 * X + 0.001 * Y)[None].astype(np.float32)
        gx, gy = rotation.local_plane_gradient(v, 500.0, 500.0)
        assert np.nanmedian(gx) == pytest.approx(0.003, rel=1e-3) and np.nanmedian(
            gy
        ) == pytest.approx(0.001, rel=1e-3)

    def test_azimuthal_shear_recovers_solid_body_rotation(self):
        for radar in ((-10.0, 0.0), (0.0, -12.0), (14.0, 9.0)):
            x, y, X, Y, vr = _solid_body(0.01, radar)
            a = rotation.azimuthal_shear(vr[None], x, y, radar)
            core = (np.abs(X) < 5000) & (np.abs(Y) < 5000)
            assert np.nanmedian(a[0][core]) == pytest.approx(
                0.01, rel=0.15
            )  # half the vorticity, any radar
        # clockwise rotation has the opposite sign
        x, y, X, Y, vr = _solid_body(-0.01)
        a = rotation.azimuthal_shear(vr[None], x, y, (-10.0, 0.0))
        assert np.nanmedian(a[0][(np.abs(X) < 5000) & (np.abs(Y) < 5000)]) < 0

    def test_vrot_of_a_couplet(self):
        x, y, X, Y, vr = _solid_body(0.01)
        v = rotation.vrot(vr, x, y, (-10.0, 0.0), baselines_km=(2.0,))
        # across 2 km the solid-body rotation has dV/2 = omega * (1 km) = 10 m/s
        assert np.nanmedian(v[(np.abs(X) < 4000) & (np.abs(Y) < 4000)]) == pytest.approx(
            10.0, rel=0.15
        )

    def test_detect_vortex_and_reject_ribbon(self):
        x = np.arange(-20, 20.01, 0.5)
        a = np.zeros((x.size, x.size), np.float32)
        a[40:43, 30:33] = 0.03  # a compact 1.5 km blob
        a[10, 5:70] = 0.03  # a 32 km long ribbon (a shear line / gridding seam)
        v = rotation.detect_vortices(a, x, x, 0.012)
        assert len(v) == 1 and rotation.detect_vortices.last_n_ribbons == 1
        assert v[0].x_km == pytest.approx(x[30:33].mean(), abs=0.6) and v[
            0
        ].peak_as == pytest.approx(0.03)

    def test_tds_flags(self):
        nz, ny, nx = 6, 30, 30
        x = np.arange(nx) * 0.5
        z = np.arange(nz) * 0.25
        dbz = np.full((nz, ny, nx), 45.0, np.float32)
        rho = np.full((nz, ny, nx), 0.97, np.float32)
        rho[0, 15, 15] = 0.6  # low RHO + high Z at the lowest level
        rho[0, 2, 2] = 0.6  # far from the vortex
        as_ll = np.zeros((ny, nx), np.float32)
        as_ll[14:17, 14:17] = 0.05
        f = rotation.tds_flag(dbz, rho, as_ll, x, x, z)
        assert f[0, 15, 15] == 2 and f[0, 2, 2] == 1 and f[0, 10, 10] == 0


# ---------------------------------------------------------------------------
# hail and convection
# ---------------------------------------------------------------------------


class TestHailConv:
    def test_isotherm_heights(self):
        h = np.array([0, 1000, 2000, 3000, 4000, 5000, 6000.0])
        t = 288.15 - 6.5e-3 * h  # 15 degC at the ground
        iso = hailconv.isotherm_heights(h, t)
        assert iso["freezing_m"] == pytest.approx(15.0 / 6.5e-3, abs=1.0)
        assert iso["minus20_m"] == pytest.approx(35.0 / 6.5e-3, abs=1.0) or iso["minus20_m"] is None

    def test_steiner_houze_yuter(self):
        z = np.full((60, 60), 30.0)  # uniform stratiform-like 30 dBZ, no peaks
        z[:8, :] = np.nan
        z[30, 30] = 52.0  # a peaked cell: more than 10 dB above the background and above 40 dBZ
        z[40:42, 10:12] = 41.0  # intense (>= 40 dBZ) always convective
        cs = hailconv.steiner_houze_yuter(z, 1.0)
        assert cs[30, 30] == 2 and cs[40, 10] == 2
        assert cs[20, 5] == 1 and cs[0, 0] == 0
        assert (cs == 2).sum() < 0.1 * (cs > 0).sum()  # convective area is small

    def test_steiner_peakedness_without_intense_core(self):
        z = np.full((40, 40), 20.0)
        z[20, 20] = (
            33.0  # 13 dB above a 20 dBZ background, below 40 dBZ: peakedness (dZ = 10 - 400/180 = 7.8 dB) triggers
        )
        cs = hailconv.steiner_houze_yuter(z, 1.0)
        assert cs[20, 20] == 2

    def test_bwer_candidate(self):
        nz, n = 41, 40
        zk = np.arange(nz) * 0.25
        dbz = np.full((nz, n, n), 45.0, np.float32)
        hole = (zk >= 1.0) & (zk <= 4.0)
        dbz[np.ix_(hole, [20], [20])] = (
            10.0  # weak echo column under a 45 dBZ overhang, ringed by strong echo
        )
        b = hailconv.bwer(dbz, zk, 0.5)
        assert b[20, 20] == 1 and b[5, 5] == 0

    def test_column_max(self):
        a = np.full((3, 2, 2), np.nan, np.float32)
        a[1, 0, 0], a[2, 0, 0], a[0, 1, 1] = 20.0, 35.0, 10.0
        m = hailconv.column_max(a)
        assert m[0, 0] == 35.0 and m[1, 1] == 10.0 and np.isnan(m[0, 1])

    @needs_radarx
    def test_hid_uses_one_legend_for_all_bands(self):
        nz, ny, nx = 12, 6, 6
        z = np.arange(nz) * 250.0
        f = {
            "dbz": np.full((nz, ny, nx), 38.0, np.float32),
            "zdr": np.full((nz, ny, nx), 1.0, np.float32),
            "rho": np.full((nz, ny, nx), 0.99, np.float32),
            "kdp": np.full((nz, ny, nx), 0.5, np.float32),
        }
        temp = hailconv.temperature_dataset(z + 100.0, 295.0 - 6.5e-3 * z)  # warm column: rain
        for band in "SCX":
            h = hailconv.unified_hid(f, z, np.arange(nx) * 500.0, np.arange(ny) * 500.0, temp, band)
            vals = set(np.unique(h[h > 0]).tolist())
            assert vals and all(v in hailconv.HID_LEGEND for v in vals), (
                band,
                vals,
            )  # only codes of the one legend
            assert (h == 2).mean() > 0.5, (
                band
            )  # 38 dBZ, ZDR 1 dB, RHO 0.99 in a warm column: mostly rain at every band
            assert 10 not in vals, (band, vals)  # no hail class in warm rain at any band


# ---------------------------------------------------------------------------
# lightning
# ---------------------------------------------------------------------------

_LMA_HEADER = """Lightning Mapping Array analyzed data
Analysis program: lma_analysis
Analysis program version: 10.14.9R
File created: Fri Apr  1 14:18:15 2022
Data start time: 03/31/22 01:10:00
Number of seconds analyzed: 600
Location: NALMA
Coordinate center (lat,lon,alt): 34.7246100 -86.6453300 0.00
Coordinate frame: cartesian
Maximum diameter of LMA (km): 90.299
Maximum light-time across LMA (ns): 301263
Number of stations: 3
Number of active stations: 3
Active stations: A B C
Minimum number of stations per solution: 6
Maximum reduced chi-squared: 5.00
Maximum number of chi-squared iterations: 20
Station information: id, name, lat(d), lon(d), alt(m), delay(ns), board_rev, rec_ch
Sta_info: A  one          34.80  -87.03   207.62  720 3  3
Sta_info: B  two          34.64  -86.77   174.40  822 3  3
Sta_info: C  three        34.72  -86.64   198.30  815 3  3
Station data: id, name, win(us), dec_win(us), data_ver, rms_error(ns), sources, %, <P/P_m>, active
Sta_data: A  one                 80    10   70     2617   4.0  0.01  A
Sta_data: B  two                 80    10   70    52148  80.5  0.02  A
Sta_data: C  three               80    10   70    42192  65.1  0.73  A
Metric file version: 4
Station mask order: NMLKJIHGFEDCBA
Data: time (UT sec of day), lat, lon, alt(m), reduced chi^2, P(dBW), mask
Data format: 15.9f 12.8f 13.8f 9.2f 6.2f 5.1f 6x
Number of events: {n}
*** data ***
"""


def _write_lma(path: Path, rows):
    with gzip.open(path, "wt") as fh:
        fh.write(_LMA_HEADER.format(n=len(rows)))
        for t, la, lo, al, chi, p, mask in rows:
            fh.write(f" {t:15.9f} {la:12.8f} {lo:13.8f} {al:9.2f} {chi:6.2f} {p:5.1f} {mask}\n")


@needs_radarx
class TestLightning:
    def test_flashes_from_sources(self, tmp_path):
        import lma

        t_day = 4200.0  # 01:10:00 UTC (seconds of the day)
        rows = []
        # flash 1: 12 sources within 2 km and 0.1 s; flash 2: 8 sources 40 km away 2 s later; a bad one (chi2 3) and a 4-station one
        for i in range(12):
            rows.append(
                (
                    t_day + 100.0 + 0.005 * i,
                    33.50 + 0.001 * i,
                    -88.50,
                    6000.0 + 50 * i,
                    0.5,
                    10.0,
                    "0xff",
                )
            )  # 8 stations
        for i in range(8):
            rows.append(
                (t_day + 102.0 + 0.005 * i, 33.90, -88.10 + 0.001 * i, 7000.0, 0.8, 12.0, "0x3f")
            )  # 6 stations
        rows.append((t_day + 100.05, 33.5, -88.5, 6000.0, 3.0, 10.0, "0xff"))  # chi2 > 1: dropped
        rows.append((t_day + 100.06, 33.5, -88.5, 6000.0, 0.5, 10.0, "0x07"))  # 3 stations: dropped
        rows.sort()
        f = tmp_path / "LYLOUT_220331_011000_0600.dat.gz"
        _write_lma(f, rows)
        ds = lma.read_sources([f])
        assert ds.sizes["number_of_events"] == 20
        c = lma.cluster(ds)
        start = datetime(2022, 3, 31, 1, 10, 0, tzinfo=UTC)
        j = lma.to_flashes_json(
            c, start + timedelta(seconds=90), start + timedelta(seconds=110), None
        )
        assert j["n_flashes"] == 2 and j["n_sources"] == 20
        f0 = j["flashes"][0]
        assert f0["n_sources"] == 12 and f0["t_start_s"] == pytest.approx(10.0, abs=0.01)
        assert f0["centroid"][0] == pytest.approx(33.5055, abs=1e-3)
        assert len(f0["sources"][0]) == 5 and j["params"]["cluster_distance_m"] == 6000.0
        # a window box that excludes the second flash
        j2 = lma.to_flashes_json(
            c,
            start + timedelta(seconds=90),
            start + timedelta(seconds=110),
            (33.0, -89.0, 33.6, -88.0),
        )
        assert j2["n_flashes"] == 1


# ---------------------------------------------------------------------------
# stations and soundings
# ---------------------------------------------------------------------------


class TestStations:
    def test_dsd_moments_of_exponential_distribution(self):
        d = np.arange(0.05, 8.0, 0.1)
        dd = np.full_like(d, 0.1)
        n0, lam = 8000.0, 2.0  # m-3 mm-1, mm-1
        nd = n0 * np.exp(-lam * d)
        m = stations.dsd_moments(nd[None], d, dd)
        assert m["dm_mm"][0] == pytest.approx(4.0 / lam, rel=0.02)
        assert 10 ** m["log10_nw"][0] == pytest.approx(n0, rel=0.03)  # Nw = N0 for an exponential
        assert m["lwc_gm3"][0] == pytest.approx(math.pi / 6 * 1e-3 * 6 * n0 / lam**4, rel=0.03)
        assert m["dmax_mm"][0] == pytest.approx(d[-1])

    def test_raindrop_fall_speed(self):
        assert stations.raindrop_fall_speed(5.0) == pytest.approx(
            9.65 - 10.3 * math.exp(-3.0), abs=1e-9
        )  # 9.14 m/s (Atlas 1973)
        assert stations.raindrop_fall_speed(0.5) < 3.0

    def test_accumulation(self):
        t = list(range(0, 3601, 10))
        assert stations.hourly_accumulation_mm(t, [6.0] * len(t)) == pytest.approx(6.0, rel=1e-6)

    def test_location_text(self):
        assert stations.eval_loc("(33.758, -88.446, 70.74)") == (33.758, -88.446, 70.74)

    def test_gust_front_passage_from_series(self):
        t = np.arange(-300, 900, 10.0)
        T = np.where(t < 300, 22.0, 18.5)  # 3.5 K drop at t = 300 s
        T = np.convolve(
            np.pad(T, 1, mode="edge"), np.ones(3) / 3, mode="valid"
        )  # a 30 s ramp, no edge artefact
        P = np.where(t < 300, 990.0, 991.8)
        ws = np.where(np.abs(t - 300) < 40, 17.0, 4.0)
        st = {
            "id": "S1",
            "lat": 33.0,
            "lon": -88.0,
            "x_km": 0.0,
            "y_km": 0.0,
            "series": {
                "t_s": t.tolist(),
                "temperature_c": T.tolist(),
                "pressure_hpa": P.tolist(),
                "wind_speed_ms": ws.tolist(),
            },
        }
        g = events.gust_front_passage(st)
        assert g is not None and abs(g["t_s"] - 300) <= 20
        assert g["dT_2min_K"] < -3.0 and g["wind_jump_ms"] == pytest.approx(13.0, abs=0.5)
        st["series"]["temperature_c"] = [22.0] * t.size  # no front
        assert events.gust_front_passage(st) is None


class TestThermo:
    def test_dewpoint_roundtrip_and_saturation(self):
        assert thermo.dewpoint_c(20.0, 100.0) == pytest.approx(20.0, abs=1e-6)
        td = thermo.dewpoint_c(25.0, 60.0)
        assert float(thermo.es_hpa(td)) == pytest.approx(0.6 * float(thermo.es_hpa(25.0)), rel=1e-6)
        assert td == pytest.approx(16.7, abs=0.2)  # standard chart value

    def test_theta_e_properties(self):
        e1 = thermo.theta_e_k(25.0, 20.0, 1000.0)
        e2 = thermo.theta_e_k(25.0, 15.0, 1000.0)
        assert e1 > e2 > thermo.theta_k(25.0, 1000.0)  # moister is warmer; always above theta
        assert thermo.theta_k(15.0, 850.0) == pytest.approx(
            288.15 * (1000 / 850) ** 0.2854, rel=1e-9
        )

    def test_parcel_path_is_pseudoadiabatic(self):
        p, t = thermo.parcel_path(1000.0, 28.0, 22.0, p_top_hpa=200.0)
        assert p[0] == 1000.0 and np.all(np.diff(p) < 0) and np.all(np.diff(t) < 0)
        dry = (28.0 + 273.15) * (p / 1000.0) ** 0.2854 - 273.15
        assert (
            t[-1] > dry[-1] + 10.0
        )  # latent heating keeps it far warmer than the dry adiabat at 200 hPa
        # lapse rate in the saturated part is far smaller than the dry 9.8 K/km at low levels
        i = int(np.argmin(np.abs(p - 800.0)))
        j = int(np.argmin(np.abs(p - 700.0)))
        assert (t[i] - t[j]) < (dry[i] - dry[j]) * 0.75


# ---------------------------------------------------------------------------
# tornadoes and tracking
# ---------------------------------------------------------------------------


class TestTornadoes:
    NSED = [
        {
            "iop": "Y1IOP2",
            "event_id": "1",
            "episode_id": "9",
            "begin": "2022-03-31 01:08:00",
            "end": "2022-03-31 01:20:00",
            "ef": "EF2",
            "blat": "33.10",
            "blon": "-88.47",
            "elat": "33.21",
            "elon": "-88.33",
            "radar": "KGWX",
            "range_km": "80",
            "narrative": "x",
        },
        {
            "iop": "Y1IOP2",
            "event_id": "2",
            "episode_id": "9",
            "begin": "2022-03-31 05:00:00",
            "end": "2022-03-31 05:05:00",
            "ef": "EF0",
            "blat": "33.1",
            "blon": "-88.4",
            "elat": "33.2",
            "elon": "-88.3",
            "radar": "KGWX",
            "range_km": "80",
            "narrative": "x",
        },
    ]
    SPC = [
        {
            "om": "621178",
            "start": "2022-03-31 01:08:00",
            "slat": "33.1008",
            "slon": "-88.4691",
            "len_mi": "10.99",
            "wid_yd": "400",
        }
    ]

    def test_build_and_interpolate(self):
        recs = tornadoes.build(
            self.NSED, self.SPC, T0, T0 + timedelta(minutes=16), (30.5, -91.0, 35.5, -86.0)
        )
        assert len(recs) == 1  # the 05:00 UTC event is 3.7 h after the window
        t = recs[0]
        assert (
            t["rating"] == "EF2"
            and t["overlaps_window"]
            and t["width_m"] == pytest.approx(365.8, abs=0.5)
        )
        assert t["max_wind_mph"] == 135 and "not a measurement" in t["wind_note"]
        assert t["path_kind"].startswith("INTERPOLATED")
        assert t["path"][0][0] == pytest.approx(240.0) and t["path"][-1][0] == pytest.approx(
            960.0
        )  # 01:08 and 01:20
        lat, lon = tornadoes.position_at(t, 600.0)  # half way
        assert lat == pytest.approx(33.155, abs=1e-6) and lon == pytest.approx(-88.40, abs=1e-6)
        assert tornadoes.position_at(t, 100.0) is None  # before the tornado


class TestTracks:
    def test_motion_of_a_shifted_blob(self):
        n = 160
        y, x = np.mgrid[0:n, 0:n] * 0.5
        a = 50.0 * np.exp(-((x - 30) ** 2 + (y - 30) ** 2) / 30.0)
        b = 50.0 * np.exp(
            -((x - 33) ** 2 + (y - 32) ** 2) / 30.0
        )  # moved 3 km east, 2 km north in 300 s
        m = tracks.estimate_motion(a, b, 300.0, 0.5, max_lag_km=10.0)
        assert m["u_ms"] == pytest.approx(10.0, abs=0.7) and m["v_ms"] == pytest.approx(
            2000 / 300.0, abs=0.7
        )
        assert m["toward_deg"] == pytest.approx(math.degrees(math.atan2(3, 2)), abs=4.0)

    def test_link_and_compare_with_survey(self):
        det = lambda x, y, p: {
            "x_km": x,
            "y_km": y,
            "peak_as": p,
            "vrot_ms": 20.0,
            "radar": "NOXP",
            "pos_unc_km": 0.75,
        }
        times = [0.0, 60.0, 120.0, 180.0]
        per_step = [
            [det(10.0, 10.0, 0.03)],
            [det(10.6, 10.0, 0.03)],
            [det(11.2, 10.1, 0.03), det(-30.0, 40.0, 0.05)],
            [],
        ]
        trs = tracks.link_vortices(per_step, times, max_km=2.0)
        longest = max(trs, key=lambda t: t["n_steps"])
        assert longest["n_steps"] == 3 and longest["duration_s"] == 120.0
        # the survey says the tornado moves 0.01 km/s east from (10.5, 10.5) at t = 0 (so 1.2 km in 120 s)
        survey = [(0.0, 10.5, 10.5), (120.0, 11.7, 10.5)]
        c = tracks.compare_with_survey(per_step, times, survey, search_km=5.0)
        assert (
            c["n_steps_detected"] == 3 and c["distance_km_median"] < 0.8
        )  # the far candidate is ignored
        assert abs(c["time_shift_s"]) <= 60.0


# ---------------------------------------------------------------------------
# winds
# ---------------------------------------------------------------------------


class TestWinds:
    def test_advect_field_moves_the_pattern(self):
        a = np.full((1, 20, 20), np.nan, np.float32)
        a[0, 10, 10] = 5.0
        b = winds.advect_field(a, 1000.0, 500.0)  # 2 cells east, 1 cell north
        j, i = np.unravel_index(int(np.nanargmax(b[0])), b[0].shape)
        assert (i, j) == (12, 11) and b[0, j, i] == pytest.approx(5.0)

    def test_coarsen_is_nan_aware(self):
        a = np.full((1, 9, 9), np.nan, np.float32)
        a[0, 3:6, 3:6] = 8.0
        c = winds.coarsen(a)
        assert c.shape == (1, 5, 5) and c[0, 2, 2] == pytest.approx(8.0) and np.isnan(c[0, 0, 0])

    @needs_radarx
    def test_uniform_wind_is_recovered_by_two_radars(self):
        # a uniform wind (u, v) = (12, 5) m/s seen by two radars at right angles; no rain (dbz low): no fall speed
        nz, n = 6, 41
        x = (np.arange(n) - n // 2) * 500.0
        z = np.arange(nz) * 500.0 + 200.0
        X, Y = np.meshgrid(x, x)
        u0, v0 = 12.0, 5.0
        rad = []
        for name, (rx, ry) in (("A", (-15000.0, 0.0)), ("B", (0.0, -15000.0))):
            R = np.hypot(X - rx, Y - ry)
            az_x, az_y = (X - rx) / R, (Y - ry) / R
            # vertical-plane geometry: the radar looks nearly horizontally (elevation ~ 0 at these ranges)
            vr = (u0 * az_x + v0 * az_y).astype(np.float32)
            vel = np.broadcast_to(vr, (nz, n, n)).copy()
            dbz = np.full((nz, n, n), 10.0, np.float32)
            rad.append(
                {
                    "name": name,
                    "vel": vel,
                    "dbz": dbz,
                    "x_m": rx,
                    "y_m": ry,
                    "alt_m": 200.0,
                    "offset_s": 0.0,
                }
            )
        arrays, summ = winds.synthesize(rad, z, x, x, 3600.0, (0.0, 0.0), max_iterations=200)
        uu = arrays["u"]
        core = np.isfinite(uu)
        assert core.sum() > 200
        assert np.nanmedian(uu[core]) == pytest.approx(u0, abs=1.0)
        assert np.nanmedian(arrays["v"][core]) == pytest.approx(v0, abs=1.0)
        assert (
            abs(np.nanmedian(arrays["vorticity"][core].astype(float))) < 5e-4
        )  # a uniform flow has no vorticity
        assert summ["valid_fraction"] > 0.1 and summ["vr_residual_rms_ms"]["A"] < 1.5


# ---------------------------------------------------------------------------
# context, bundle files
# ---------------------------------------------------------------------------


class TestContextAndFiles:
    def test_barnes_constant_field_and_station_minimum(self):
        gx = np.arange(-20.0, 20.1, 2.0)
        x = np.array([-5.0, 5.0, 0.0])
        y = np.array([0.0, 0.0, 6.0])
        f = context.barnes(x, y, np.array([290.0, 290.0, 290.0]), gx, gx)
        assert np.nanmax(np.abs(f[np.isfinite(f)] - 290.0)) < 1e-4 and np.isfinite(f).sum() > 20
        assert np.isnan(f[0, 0])  # far from every station: no value
        assert np.isnan(
            context.barnes(x[:1], y[:1], np.array([1.0]), gx, gx)
        ).all()  # fewer than 2 stations

    def test_wind_components(self):
        u, v = context.wind_components(10.0, 270.0)  # from the west: toward the east
        assert u == pytest.approx(10.0) and abs(v) < 1e-9

    def test_json_has_no_nan(self, tmp_path):
        p = tmp_path / "x.json"
        bundle.write_json(
            p,
            bundle.clean_nan(
                {"a": float("nan"), "b": [1.0, float("inf"), np.float32(2.5)], "c": np.int64(3)}
            ),
        )
        d = json.loads(p.read_text())
        assert d == {"a": None, "b": [1.0, None, 2.5], "c": 3}

    def test_sources_are_classified(self):
        classes = {s["class"] for s in bundle.SOURCES}
        assert classes <= {"observed", "model", "analysis", "retrieved"}
        assert {"observed", "retrieved", "analysis"} <= classes
        for s in bundle.SOURCES:
            assert all(
                s[k] for k in ("name", "class", "instrument", "credit", "licence_note", "what")
            )

    def test_hid_legend_is_complete(self):
        leg = bundle.hid_legend()
        assert set(leg["codes"]) == {str(k) for k in hailconv.HID_LEGEND}
        assert set(leg["methods"]) == {"S", "C", "X"}

    def test_save_step_keeps_spec_names_first(self, tmp_path):
        arr = {
            "zz_extra": np.zeros((2, 2), np.uint8),
            "dbz": np.zeros((2, 2, 2), np.float32),
            "nradars": np.zeros((2, 2, 2), np.uint8),
            "rho": np.ones((2, 2, 2), np.float32),
        }
        n = volumes.save_step(tmp_path / "v" / "step_0000.npz", arr, "2022-03-31T01:04:00Z")
        assert n > 0
        z = np.load(tmp_path / "v" / "step_0000.npz")
        assert next(iter(z.files)) == "dbz"  # the SPEC v1 names come first
        assert (
            z["rho"].dtype == np.float16
            and z["dbz"].dtype == np.float32
            and str(z["time_iso"]) == "2022-03-31T01:04:00Z"
        )


# ---------------------------------------------------------------------------
# one whole step on synthetic radars
# ---------------------------------------------------------------------------


@needs_radarx
def test_build_step_end_to_end_on_synthetic_radars():
    """Two synthetic radars see the same blob; the merged volume, shear and diagnostics are consistent."""
    nz, ny, nx = volumes.NZ, volumes.NY, volumes.NX
    z = volumes.Z_M
    X, Y = np.meshgrid(volumes.X_KM, volumes.Y_KM)
    blob = np.exp(-((X - 10.0) ** 2 + (Y - 5.0) ** 2) / 50.0)
    base = np.where(z[:, None, None] < 8000.0, 1.0, 0.0)
    dbz = (15.0 + 40.0 * blob[None] * base).astype(np.float32)
    roi = np.full((nz, ny, nx), 1000.0, np.float32)
    fields = {
        "dbz": dbz,
        "roi": roi,
        "vel": np.full((nz, ny, nx), 8.0, np.float32),
        "zdr": np.full((nz, ny, nx), 0.5, np.float32),
        "rho": np.full((nz, ny, nx), 0.99, np.float32),
        "kdp": np.full((nz, ny, nx), 0.2, np.float32),
    }
    env = volumes.Env(z + 100.0, 295.0 - 6.5e-3 * z, 3600.0, 6800.0, "synthetic")
    plan = merge.StepPlan(
        0, T0, {"KGWX": merge.VolumeRef("KGWX", Path("a.nc"), T0, T0)}, {"KGWX": 0.0}
    )
    g = pgrid.RadarGrid("KGWX", Path("a.nc"), T0, T0, 33.9, -88.3, 179.0, fields)
    prods = {"KGWX": volumes.Prod("KGWX", g, None, None, None, (20.0, 36.0))}
    arrays, summ = volumes.build_step(plan, prods, ["COW1", "KGWX"], env, pgrid.QCConfig())
    assert arrays["dbz"].shape == (nz, ny, nx) and arrays["nradars"].max() == 1
    # a single radar: the merge returns its reflectivity (down to float rounding) where it is above 0 dBZ
    np.testing.assert_allclose(arrays["dbz"][0, 180, 200], dbz[0, 180, 200], atol=1e-3)
    assert arrays["vel_src"].max() == 2  # KGWX is the second name of the list
    assert summ["cmax_dbz"] == pytest.approx(55.0, abs=0.3)
    assert summ["radars_used"]["KGWX"]["dbz"] > 1000
    cs = arrays["conv_strat"]
    assert (
        0 < (cs == 2).sum() < (cs == 1).sum()
    )  # the 40+ dBZ core is convective, the 15 dBZ rest stratiform
    assert np.isfinite(arrays["echotop30_km"]).any() and summ["echotop30_max_km"] > 7.0
    assert arrays["tds"].max() == 0  # no low RHO anywhere

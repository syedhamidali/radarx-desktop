"""Hydrology QPE: tests with known answers (exact DSD truth, constant rate, translating blob).

Tolerances are the stated accuracy of the method, not arbitrary: they are the numbers in
``docs/HYDROLOGY.md`` (section "Accuracy against a known truth").
"""

from __future__ import annotations

import itertools
import math
from typing import ClassVar

import numpy as np
import pytest
import xarray as xr

pytest.importorskip("radarx")

from engine.hydro import accumulate as acc
from engine.hydro import grid as hgrid
from engine.hydro import qpe
from tests.hydro_synth import make_reader, z_of_r

LAT, LON = 35.0, -97.0


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


class TestRelations:
    @pytest.mark.parametrize("key", list(qpe.ZR_RELATIONS))
    def test_zr_inverse_is_exact(self, key):
        rel = qpe.ZR_RELATIONS[key]
        r = np.array([0.1, 1.0, 10.0, 100.0])
        z = rel.A * r**rel.beta
        assert rel.a * z**rel.b == pytest.approx(r, rel=1e-12)

    def test_default_is_wsr88d_and_matches_radarx(self):
        from radarx.retrieve import qpe as rq

        rel = qpe.zr_relation(None)
        assert (rel.A, rel.beta) == (300.0, 1.4)
        assert rel.a == pytest.approx(rq.COEFFICIENTS["S"]["z"][0], rel=2e-3)
        assert rel.b == pytest.approx(rq.COEFFICIENTS["S"]["z"][1], rel=2e-3)

    def test_snow_and_rain_are_not_mixed_up(self):
        with pytest.raises(qpe.QpeError, match="snow relation"):
            qpe.zr_relation("snow_sekhon_srivastava")
        assert qpe.zr_relation(None, snow=True).kind == "snow"
        with pytest.raises(qpe.QpeError, match="unknown Z-R"):
            qpe.zr_relation("nope")

    def test_config_validation(self):
        for bad in (
            {"method": "magic"},
            {"band": "K"},
            {"precip": "hail"},
            {"zr_a": 200.0},
            {"r_light": 60.0, "r_heavy": 50.0},
            {"nonsense": 1},
        ):
            with pytest.raises(qpe.QpeError):
                qpe.QpeConfig.from_dict(bad)

    def test_beam_geometry_matches_the_textbook(self):
        # Doviak & Zrnic: 0.5 deg, 100 km -> height 1.5 km above the radar, ground range ~ 99.97 km
        s, h = qpe.beam_geometry(np.array([100_000.0]), 0.5)
        assert h[0] == pytest.approx(1.5e3, rel=0.06)
        assert s[0] == pytest.approx(99_986.0, abs=120.0)
        # independent evaluation of the same model
        rp = 4 / 3 * 6371e3
        rho = math.sqrt(1e10 + rp**2 + 2 * 1e5 * rp * math.sin(math.radians(0.5)))
        assert h[0] == pytest.approx(rho - rp, abs=1e-6)


# ---------------------------------------------------------------------------
# DSD truth: Z, ZDR, KDP of gamma DSDs (radarx forward model) -> rain rate
# ---------------------------------------------------------------------------


def _dsd_sweep(n=2400, seed=7):
    import radarx.retrieve as rt

    rng = np.random.default_rng(seed)
    mu = 3.0
    nw = 10 ** rng.uniform(3.0, 4.2, n)
    dm = rng.uniform(0.9, 2.6, n)
    f = 6.0 / 4**4 * (4 + mu) ** (mu + 4) / math.gamma(mu + 4)
    params = xr.Dataset(
        {
            "N0": ("g", nw * f / dm**mu),
            "MU": ("g", np.full(n, mu)),
            "LAMBDA": ("g", (4 + mu) / dm),
        }
    )
    rv = rt.radar_from_dsd(rt.dsd_spectrum(params, np.arange(0.1, 8.0, 0.1)), band="S")
    na = 24
    nr = n // na

    def sh(a):
        return np.asarray(a.values if hasattr(a, "values") else a)[: na * nr].reshape(na, nr)

    ds = xr.Dataset(
        {k: (("azimuth", "range"), sh(rv[k])) for k in ("DBZH", "ZDR", "KDP", "RHOHV")},
        coords={
            "azimuth": np.arange(na) * (360.0 / na) + 0.5,
            "range": 2000 + 250.0 * np.arange(nr),
            "elevation": ("azimuth", np.full(na, 0.5)),
            "latitude": LAT,
            "longitude": LON,
            "altitude": 300.0,
        },
    )
    return ds, sh(rv["RAIN_RATE"])


@pytest.fixture(scope="module")
def dsd():
    return _dsd_sweep()


def _score(ds, truth, lo, hi, **cfg):
    base = {"min_dbz": -50.0, "z_cap": None, "use_hid": False}
    sq = qpe.estimate_sweep(ds, {**base, **cfg})
    est = sq.rate.astype(float)
    m = (truth >= lo) & (truth < hi) & np.isfinite(est)
    e, t = est[m], truth[m]
    return m.sum(), float(np.mean(e - t) / np.mean(t)), float(np.mean(np.abs(e - t)) / np.mean(t))


class TestAccuracyAgainstDsdTruth:
    """Rain rate recovered from Z/ZDR/KDP of 2400 gamma DSDs (Dm 0.9-2.6 mm, Nw 10^3-10^4.2).

    The truth is the rain rate of the DSD itself. The S-band relations are the published Oklahoma
    ones, so a bias of the order of 10-15 % against this broad synthetic ensemble is expected and
    is exactly the representativeness limit that the uncertainty fields describe.
    """

    @pytest.mark.parametrize(
        ("method", "lo", "hi", "max_bias", "max_nmae"),
        [
            ("z", 0.5, 400, 0.12, 0.30),
            ("z_zdr", 0.5, 400, 0.20, 0.25),
            ("kdp", 5.0, 400, 0.20, 0.20),
            ("hybrid", 0.5, 5, 0.12, 0.30),
            ("hybrid", 5, 25, 0.12, 0.20),
            ("hybrid", 25, 400, 0.20, 0.20),
        ],
    )
    def test_methods(self, dsd, method, lo, hi, max_bias, max_nmae):
        ds, truth = dsd
        n, bias, nmae = _score(ds, truth, lo, hi, method=method)
        assert n > 200
        assert abs(bias) <= max_bias, f"{method} bias {bias:+.3f}"
        assert nmae <= max_nmae, f"{method} nmae {nmae:.3f}"

    def test_hybrid_beats_rz_in_moderate_rain(self, dsd):
        """Where ZDR adds information (moderate rain) the hybrid clearly improves on R(Z)."""
        ds, truth = dsd
        _, _, nmae_z = _score(ds, truth, 5, 25, method="z")
        _, _, nmae_h = _score(ds, truth, 5, 25, method="hybrid")
        assert nmae_h < 0.7 * nmae_z

    def test_hybrid_with_hid_stays_accurate(self, dsd):
        ds, truth = dsd
        n, bias, nmae = _score(ds, truth, 5, 400, method="hybrid", use_hid=True)
        assert n > 300
        assert abs(bias) < 0.15
        assert nmae < 0.22

    def test_relation_codes_follow_the_regimes(self, dsd):
        ds, _ = dsd
        sq = qpe.estimate_sweep(ds, {"method": "hybrid", "min_dbz": -50.0, "use_hid": False})
        r_z = 300.0 ** (-1 / 1.4) * 10 ** (np.nan_to_num(ds["DBZH"].values) / 10 / 1.4)
        light = (r_z < 5.5) & (sq.relation > 0)
        assert (sq.relation[light] == qpe.REL_Z).all()
        assert (sq.relation == qpe.REL_KDP).any()
        assert (sq.relation == qpe.REL_ZDR).any()

    def test_uncertainty_is_positive_and_ordered(self, dsd):
        ds, _ = dsd
        sz = qpe.estimate_sweep(ds, {"method": "z", "min_dbz": -50.0})
        sx = qpe.estimate_sweep(ds, {"method": "z_zdr", "min_dbz": -50.0})
        a, b = sz.sigma[np.isfinite(sz.sigma)], sx.sigma[np.isfinite(sx.sigma)]
        assert (a > 0).all() and (b > 0).all()
        # R(Z) is the least constrained relation: about 0.45 (DSD) vs 0.1-0.2 for R(Z,ZDR)
        assert a.mean() > 2 * b.mean()
        expected = math.sqrt(0.455**2 + (0.714 * math.log(10) / 10 * 1.0) ** 2)
        assert a.mean() == pytest.approx(expected, rel=0.08)

    def test_exact_inversion_of_the_default_relation(self):
        """Z = 300 R^1.4 in, the same rate out (WSR-88D relation is exact, not fitted)."""
        r = np.array([[0.2, 1.0, 5.0, 20.0, 60.0]])
        ds = xr.Dataset(
            {"DBZH": (("azimuth", "range"), z_of_r(r))},
            coords={
                "azimuth": [10.0],
                "range": 2000 + 250.0 * np.arange(5),
                "elevation": ("azimuth", [0.5]),
                "latitude": LAT,
                "longitude": LON,
                "altitude": 300.0,
            },
        )
        sq = qpe.estimate_sweep(ds, {"method": "z", "min_dbz": -30.0})
        assert sq.rate[0] == pytest.approx(r[0], rel=2e-3)

    def test_z_cap_limits_hail_contamination(self):
        ds = xr.Dataset(
            {"DBZH": (("azimuth", "range"), np.array([[45.0, 53.0, 60.0, 70.0]]))},
            coords={
                "azimuth": [10.0],
                "range": 2000 + 250.0 * np.arange(4),
                "elevation": ("azimuth", [0.5]),
                "latitude": LAT,
                "longitude": LON,
                "altitude": 300.0,
            },
        )
        sq = qpe.estimate_sweep(ds, {"method": "z"})
        assert sq.rate[0, 2] == pytest.approx(sq.rate[0, 1], rel=1e-5)  # capped at 53 dBZ
        assert sq.rate[0, 3] == pytest.approx(sq.rate[0, 1], rel=1e-5)
        assert (sq.flags[0, 2:] & qpe.FLAG_ZCAP).all()
        free = qpe.estimate_sweep(ds, {"method": "z", "z_cap": None})
        assert free.rate[0, 3] > 5 * free.rate[0, 1]

    def test_snow_zs_relation(self):
        # S = (Z / 1780)^(1/2.21): Z = 1780 -> 1 mm/h liquid equivalent
        ds = xr.Dataset(
            {"DBZH": (("azimuth", "range"), np.array([[10 * math.log10(1780.0)]]))},
            coords={
                "azimuth": [10.0],
                "range": [5000.0],
                "elevation": ("azimuth", [0.5]),
                "latitude": LAT,
                "longitude": LON,
                "altitude": 300.0,
            },
        )
        sq = qpe.estimate_sweep(ds, {"precip": "snow", "min_dbz": 0.0})
        assert sq.rate[0, 0] == pytest.approx(1.0, rel=5e-3)
        assert sq.relation[0, 0] == qpe.REL_SNOW
        assert sq.sigma[0, 0] > 0.6  # snow Z-S is the least certain relation

    def test_melting_layer_gating_and_flags(self):
        r = np.full((1, 6), 5.0)
        ds = xr.Dataset(
            {"DBZH": (("azimuth", "range"), z_of_r(r))},
            coords={
                "azimuth": [10.0],
                "range": np.array([5e3, 20e3, 50e3, 100e3, 150e3, 200e3]),
                "elevation": ("azimuth", [1.0]),
                "latitude": LAT,
                "longitude": LON,
                "altitude": 300.0,
            },
        )
        sq = qpe.estimate_sweep(ds, {"method": "z", "freezing_level_m": 2500.0})
        h = sq.beam_height_m + 300.0
        ok = np.isfinite(sq.rate[0]) & (sq.rate[0] > 0)
        assert ok[0] and not ok[-1]  # low beam estimated, high beam above the freezing level
        assert (sq.flags[0][~ok] & qpe.FLAG_ML).all()
        assert (h[~ok] > 2000).all()
        flagged = qpe.estimate_sweep(
            ds, {"method": "z", "freezing_level_m": 2500.0, "ml_policy": "flag"}
        )
        assert np.isfinite(flagged.rate[0]).all()
        assert (flagged.flags[0][~ok] & qpe.FLAG_ML).all()

    def test_attenuation_flag_only_with_phase(self):
        sq = qpe.estimate_sweep(
            xr.Dataset(
                {"DBZH": (("azimuth", "range"), np.array([[30.0, 35.0]]))},
                coords={
                    "azimuth": [1.0],
                    "range": [3e3, 9e3],
                    "elevation": ("azimuth", [0.5]),
                    "latitude": LAT,
                    "longitude": LON,
                    "altitude": 0.0,
                },
            ),
            {"method": "z"},
        )
        assert not (sq.flags & qpe.FLAG_ATTEN).any()
        assert any("No DEM" in n for n in sq.meta["notes"])


class TestPhaseAndKdpRules:
    def _sweep(self, dbz, zdr, rhohv, kdp):
        n = len(dbz)
        return xr.Dataset(
            {
                "DBZH": (("azimuth", "range"), np.array([dbz], dtype=float)),
                "ZDR": (("azimuth", "range"), np.array([zdr], dtype=float)),
                "RHOHV": (("azimuth", "range"), np.array([rhohv], dtype=float)),
                "KDP": (("azimuth", "range"), np.array([kdp], dtype=float)),
            },
            coords={
                "azimuth": [10.0],
                "range": 3000 + 500.0 * np.arange(n),
                "elevation": ("azimuth", [0.5]),
                "latitude": LAT,
                "longitude": LON,
                "altitude": 300.0,
            },
        )

    def test_kdp_below_its_noise_floor_is_no_estimate_not_a_small_rate(self):
        ds = self._sweep([30.0] * 3, [0.5] * 3, [0.99] * 3, [0.05, 0.5, 2.0])
        sq = qpe.estimate_sweep(ds, {"method": "kdp", "min_dbz": 0.0})
        assert np.isnan(sq.rate[0, 0]) and (sq.flags[0, 0] & qpe.FLAG_NOEST)
        # R = 44 |KDP|^0.822 (Giangrande & Ryzhkov 2008, S band)
        assert sq.rate[0, 1] == pytest.approx(44.0 * 0.5**0.822, rel=1e-4)
        assert sq.rate[0, 2] == pytest.approx(44.0 * 2.0**0.822, rel=1e-4)

    def test_without_a_freezing_level_ice_classes_are_estimated_as_rain_and_flagged(self):
        # weak echo, ZDR ~ 0, high RhoHV: the classification (no temperature) calls it snow / ice
        n = 8
        ds = self._sweep([14.0] * n, [0.05] * n, [0.995] * n, [0.0] * n)
        excl = qpe.estimate_sweep(ds, {"method": "hybrid", "phase_policy": "exclude", "min_dbz": 5})
        auto = qpe.estimate_sweep(ds, {"method": "hybrid", "min_dbz": 5})
        dropped = np.isnan(excl.rate)
        assert dropped.any(), "the test gates should be classified as ice-phase"
        assert np.isfinite(auto.rate[dropped]).all()
        assert (auto.flags[dropped] & qpe.FLAG_PHASE).all()
        assert not (excl.flags[dropped] & qpe.FLAG_PHASE).any()
        # with a (low) freezing level the same gates are excluded, as the class says
        low = qpe.estimate_sweep(
            ds,
            {"method": "hybrid", "min_dbz": 5, "freezing_level_m": 400.0, "ml_policy": "exclude"},
        )
        assert np.isnan(low.rate[dropped]).all()

    def test_phase_policy_is_validated(self):
        with pytest.raises(qpe.QpeError, match="phase_policy"):
            qpe.QpeConfig.from_dict({"phase_policy": "maybe"})


# ---------------------------------------------------------------------------
# Gridding: a cell is the mean of its gates
# ---------------------------------------------------------------------------


def _grid_for(reader, res=1000.0, rng_km=150.0):
    sc = acc.scan_meta(reader)
    bbox = hgrid.bbox_for_radars([{"latitude": sc.latitude, "longitude": sc.longitude}], rng_km)
    grid = hgrid.make_grid(bbox, res)
    return sc, grid, hgrid.RadarGeometry.build(grid, sc.latitude, sc.longitude, sc.altitude_m)


class TestGrid:
    def test_uniform_rain_is_reproduced_and_coverage_is_the_range_disc(self):
        rd = make_reader(
            LAT, LON, lambda x, y: np.full(np.broadcast(x, y).shape, 8.0), "2024-05-01T20:00:00"
        )
        sc, grid, geom = _grid_for(rd)
        cfg = qpe.QpeConfig.from_dict({"min_dbz": -30.0})
        rf, _ = acc.radar_field(sc, cfg, grid, geom, max_range_km=150.0)
        inside = geom.s_m < 140_000
        outside = geom.s_m > 160_000
        assert np.isfinite(rf.rate[inside]).all()
        assert rf.rate[inside] == pytest.approx(8.0, rel=2e-3)
        assert not np.isfinite(rf.rate[outside]).any()

    def test_cells_are_gate_means_so_the_rain_volume_is_conserved(self):
        """A 1.5 km, 100 mm/h core on 1 mm/h background: sum(rate x area) keeps the true volume."""
        core_r = 1500.0

        def hot(x, y):
            return np.where(np.hypot(x - 40_000, y) < core_r, 100.0, 1.0)

        rd = make_reader(LAT, LON, hot, "2024-05-01T20:00:00", gate_m=250.0, n_range=800, n_az=1440)
        sc, grid, geom = _grid_for(rd, 1000.0, 100.0)
        cfg = qpe.QpeConfig.from_dict({"min_dbz": -30.0, "z_cap": None})
        rf, _ = acc.radar_field(sc, cfg, grid, geom, max_range_km=100.0)
        area = grid.cell_areas_km2()
        in_range = (geom.s_m > 5_000) & (geom.s_m < 95_000)
        v_est = float(np.nansum(np.where(in_range, rf.rate * area, 0.0)))
        v_true = 1.0 * float((area * in_range).sum()) + 99.0 * math.pi * (core_r / 1000.0) ** 2
        assert v_est == pytest.approx(v_true, rel=0.01)
        assert 20.0 < np.nanmax(rf.rate) <= 100.01  # a 1 km cell sees part of the core

    def test_best_beam_prefers_the_lowest_unblocked_beam(self):
        rd = make_reader(
            LAT, LON, lambda x, y: np.full(np.broadcast(x, y).shape, 4.0), "2024-05-01T20:00:00"
        )
        sc, grid, geom = _grid_for(rd)
        cfg = qpe.QpeConfig.from_dict({"min_dbz": -30.0})
        rf, _ = acc.radar_field(sc, cfg, grid, geom, n_sweeps=2, max_range_km=150.0)
        assert (rf.beam[np.isfinite(rf.rate)] == 0).all()
        assert np.nanmax(rf.quality) <= 1.0
        # quality falls with range
        r = np.where(np.isfinite(rf.rate), rf.range_km, np.nan)
        q = rf.quality
        near = np.nanmean(q[(r > 20) & (r < 40)])
        far = np.nanmean(q[(r > 100) & (r < 120)])
        assert near > far


# ---------------------------------------------------------------------------
# Accumulation: integral of the rate
# ---------------------------------------------------------------------------


def _const(rate):
    return lambda x, y: np.full(np.broadcast(x, y).shape, rate)


def _times(minutes, base="2024-05-01T20:00:00"):
    t0 = np.datetime64(base)
    return [str(t0 + np.timedelta64(int(m * 60), "s")) for m in minutes]


class TestAccumulation:
    CFG: ClassVar[dict] = {"min_dbz": -30.0}

    def _acc(self, rate, minutes, **kw):
        readers = [make_reader(LAT, LON, _const(rate), t) for t in _times(minutes)]
        return acc.accumulate(readers, self.CFG, resolution_m=2000.0, max_range_km=120.0, **kw)

    @pytest.mark.parametrize("minutes", [[0, 5, 10, 15, 20], [0, 3, 10, 15, 21]])
    def test_constant_rate_gives_rate_times_time(self, minutes):
        """R = 10 mm/h for T hours gives exactly R T, whatever the (irregular) scan spacing."""
        res = self._acc(10.0, minutes)
        wet = np.isfinite(res.accumulation) & (res.range_km > 10) & (res.range_km < 100)
        expected = 10.0 * (minutes[-1] - minutes[0]) / 60.0
        assert res.duration_s == pytest.approx((minutes[-1] - minutes[0]) * 60.0)
        assert wet.sum() > 1000
        assert res.accumulation[wet] == pytest.approx(expected, rel=2e-3)
        assert res.valid_fraction[wet] == pytest.approx(1.0)
        assert res.rate_max[wet] == pytest.approx(10.0, rel=2e-3)

    def test_sampling_interval_does_not_change_the_answer(self):
        a = self._acc(6.0, [0, 5, 10, 15], step_s=30.0)
        b = self._acc(6.0, [0, 5, 10, 15], step_s=300.0)
        w = np.isfinite(a.accumulation) & np.isfinite(b.accumulation)
        assert a.accumulation[w] == pytest.approx(b.accumulation[w], rel=1e-4)

    def test_a_long_gap_is_missing_not_filled(self):
        res = self._acc(10.0, [0, 5, 40, 45], max_gap_s=900.0)
        wet = np.isfinite(res.accumulation) & (res.range_km > 10) & (res.range_km < 100)
        # 10 of 45 minutes observed: 5 + 5 min
        assert res.valid_fraction[wet] == pytest.approx(10.0 / 45.0, rel=1e-3)
        assert res.accumulation[wet] == pytest.approx(10.0 * 10 / 60.0, rel=2e-3)
        assert any("not bridged" in n for n in res.notes)

    def test_period_longer_than_the_scans_is_not_extrapolated(self):
        res = self._acc(10.0, [0, 10], start="2024-05-01T19:50:00", end="2024-05-01T20:20:00")
        wet = np.isfinite(res.accumulation) & (res.range_km > 10) & (res.range_km < 100)
        assert res.valid_fraction[wet] == pytest.approx(10.0 / 30.0, rel=1e-3)
        assert any("before the first scan" in n for n in res.notes)

    def test_single_volume_is_an_instant_composite(self):
        res = self._acc(7.0, [0])
        wet = np.isfinite(res.rate_last) & (res.range_km > 10)
        assert res.duration_s == 0
        assert res.rate_last[wet] == pytest.approx(7.0, rel=2e-3)
        assert any("0 mm over 0 s" in n for n in res.notes)

    def test_uncertainty_is_reported_and_scales_with_the_amount(self):
        res = self._acc(10.0, [0, 10, 20])
        wet = np.isfinite(res.accumulation_sigma) & (res.accumulation > 1)
        rel = res.accumulation_sigma[wet] / res.accumulation[wet]
        # R(Z): sigma(ln R) = sqrt(0.455^2 + (0.714 ln10/10)^2) ~ 0.48
        assert rel.mean() == pytest.approx(0.48, abs=0.05)

    def test_provenance_is_retrieved_never_observed(self):
        res = self._acc(10.0, [0, 10])
        p = res.provenance()
        assert p["type"] == "RETRIEVED"
        assert "WSR-88D" in str(p["method"]) or "300" in str(p["method"])
        assert p["uncertainty"]["kind"].startswith("1-sigma")
        assert "OBSERVED" not in str(p)


class TestAdvection:
    """A Gaussian storm moving at 15 m/s: the accumulation must follow the track."""

    U = 15.0

    def _readers(self, times_s):
        def storm(t):
            def fn(x, y):
                return 30.0 * np.exp(
                    -((x - (-20_000.0 + self.U * t)) ** 2 + (y - 10_000.0) ** 2) / (2 * 8000.0**2)
                )

            return fn

        t0 = np.datetime64("2024-05-01T20:00:00")
        return [
            make_reader(LAT, LON, storm(t), str(t0 + np.timedelta64(int(t), "s")), gate_m=500.0)
            for t in times_s
        ], storm

    def _truth(self, res, storm, t_end):
        g = res.grid
        xx, yy = np.meshgrid(g.x, g.y)
        # the radar sits at the origin of its own AEQD; the grid origin is the box centre
        import pyproj

        tr = pyproj.Transformer.from_crs("EPSG:4326", g.proj4(), always_xy=True)
        rx, ry = tr.transform(LON, LAT)
        acc_t = np.zeros_like(xx)
        ts = np.arange(0.0, t_end + 1e-9, 5.0)
        for a, b in itertools.pairwise(ts):
            acc_t += (
                0.5 * (storm(a)(xx - rx, yy - ry) + storm(b)(xx - rx, yy - ry)) * (b - a) / 3600.0
            )
        return acc_t

    def test_advection_follows_the_storm_track(self):
        readers, storm = self._readers([0.0, 600.0])
        cfg = {"min_dbz": -30.0, "z_cap": None}
        kw = {"resolution_m": 1000.0, "max_range_km": 100.0, "step_s": 30.0, "max_gap_s": 900.0}
        with_adv = acc.accumulate(readers, cfg, advect=True, **kw)
        lin = acc.accumulate(readers, cfg, advect=False, **kw)
        truth = self._truth(with_adv, storm, 600.0)
        domain = np.isfinite(with_adv.accumulation) & (with_adv.range_km > 5)
        e_adv = np.sqrt(np.mean((with_adv.accumulation[domain] - truth[domain]) ** 2))
        e_lin = np.sqrt(np.mean((lin.accumulation[domain] - truth[domain]) ** 2))
        peak = truth[domain].max()
        assert e_adv < 0.06 * peak, f"advection rmse {e_adv:.3f} vs peak {peak:.2f}"
        assert e_adv < 0.5 * e_lin  # clearly better than a linear blend in time
        # rain volume is conserved (to the gridding error)
        area = with_adv.grid.cell_areas_km2()
        v_true = float((truth[domain] * area[domain]).sum())
        v_adv = float((with_adv.accumulation[domain] * area[domain]).sum())
        assert v_adv == pytest.approx(v_true, rel=0.04)
        # the retrieved storm motion is the true one
        ux = with_adv.radars[0]["motion_mean_ms"]
        assert ux is not None
        assert ux[0] == pytest.approx(self.U, abs=1.5)
        assert abs(ux[1]) < 1.5

    def test_motion_uses_pseudo_reflectivity_of_the_rate(self):
        readers, _ = self._readers([0.0, 600.0])
        res = acc.accumulate(
            readers,
            {"min_dbz": -30.0, "z_cap": None},
            resolution_m=1000.0,
            max_range_km=100.0,
            step_s=60.0,
        )
        assert res.options["advect"] is True
        assert res.radars[0]["scans"] == 2


# ---------------------------------------------------------------------------
# Compositing several radars
# ---------------------------------------------------------------------------


class TestComposite:
    def test_two_radars_agree_on_uniform_rain_and_overlap_is_counted(self):
        r1 = make_reader(35.0, -97.0, _const(6.0), "2024-05-01T20:00:00", name="AAA")
        r2 = make_reader(35.0, -95.9, _const(6.0), "2024-05-01T20:00:00", name="BBB")
        res = acc.accumulate([r1, r2], {"min_dbz": -30.0}, resolution_m=2000.0, max_range_km=100.0)
        fin = np.isfinite(res.rate_last)
        assert res.rate_last[fin] == pytest.approx(6.0, rel=3e-3)
        assert res.n_radars.max() == 2
        both = res.n_radars == 2
        assert both.sum() > 100
        assert len(res.radars) == 2

    def test_best_beam_and_weighted_pick_by_quality(self):
        r1 = make_reader(35.0, -97.0, _const(10.0), "2024-05-01T20:00:00", name="AAA")
        r2 = make_reader(35.0, -95.9, _const(2.0), "2024-05-01T20:00:00", name="BBB")
        kw = {"resolution_m": 2000.0, "max_range_km": 100.0}
        res_best = acc.accumulate([r1, r2], {"min_dbz": -30.0}, composite="best", **kw)
        res_w = acc.accumulate([r1, r2], {"min_dbz": -30.0}, composite="weighted", **kw)
        # near radar A (west) the answer is A's 10 mm/h, near B (east) B's 2 mm/h
        g = res_best.grid
        import pyproj

        tr = pyproj.Transformer.from_crs("EPSG:4326", g.proj4(), always_xy=True)
        ax, ay = tr.transform(-97.0, 35.0)
        bx, by = tr.transform(-95.9, 35.0)
        ia, ja = round((ax - g.x[0]) / g.cell_m) + 8, round((ay - g.y[0]) / g.cell_m)
        ib, jb = round((bx - g.x[0]) / g.cell_m) - 8, round((by - g.y[0]) / g.cell_m)
        assert res_best.rate_last[ja, ia] == pytest.approx(10.0, rel=0.01)
        assert res_best.rate_last[jb, ib] == pytest.approx(2.0, rel=0.01)
        mid = res_w.rate_last
        fin = np.isfinite(mid) & (res_w.n_radars == 2)
        assert (mid[fin] > 2.0).all() and (mid[fin] < 10.0).all()


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------


class TestBeamBlockage:
    def test_ridge_blocks_the_low_beam_and_the_compositor_uses_the_next_one(self):
        lat0, lon0 = 35.0, -97.0
        lats = np.arange(34.0, 36.0, 0.01)
        lons = np.arange(-98.2, -95.8, 0.01)
        # a N-S ridge 30 km east of the radar, 1500 m high
        east_km = (lons - lon0) * 111.32 * math.cos(math.radians(lat0))
        ridge = 1500.0 * np.exp(-((east_km - 30.0) ** 2) / (2 * 2.0**2))
        dem = xr.DataArray(
            np.tile(ridge, (lats.size, 1)),
            dims=("latitude", "longitude"),
            coords={"latitude": lats, "longitude": lons},
        )
        rd = make_reader(lat0, lon0, _const(5.0), "2024-05-01T20:00:00", elevations=(0.5, 3.0, 6.0))
        res = acc.accumulate(
            [rd],
            {"min_dbz": -30.0, "dem": dem, "correct_blockage": False},
            resolution_m=1000.0,
            max_range_km=100.0,
            n_sweeps=3,
        )
        assert res.provenance()["parameters"]["dem"] is True
        shadow = (res.range_km > 40) & (res.range_km < 80) & np.isfinite(res.rate_last)
        assert shadow.sum() > 100
        assert res.quality is not None
        # the rain rate in the shadow is the true one: the higher beam served those cells
        assert res.rate_last[shadow] == pytest.approx(5.0, rel=0.02)

    def test_blockage_fields_flag_and_lower_quality(self):
        lat0, lon0 = 35.0, -97.0
        lats = np.arange(34.0, 36.0, 0.01)
        lons = np.arange(-98.2, -95.8, 0.01)
        east_km = (lons - lon0) * 111.32 * math.cos(math.radians(lat0))
        dem = xr.DataArray(
            np.tile(1500.0 * np.exp(-((east_km - 30.0) ** 2) / 8.0), (lats.size, 1)),
            dims=("latitude", "longitude"),
            coords={"latitude": lats, "longitude": lons},
        )
        rd = make_reader(lat0, lon0, _const(5.0), "2024-05-01T20:00:00", elevations=(0.5,))
        ds = acc.sweep_dataset(rd, 0)
        site = {"latitude": lat0, "longitude": lon0, "altitude": 100.0}
        sq = qpe.estimate_sweep(ds, {"min_dbz": -30.0, "dem": dem}, site=site)
        assert sq.blockage is not None
        east = np.argmin(np.abs(sq.azimuth - 90.0))
        r = sq.ground_range_m
        behind = (r > 40e3) & (r < 80e3)
        before = (r > 10e3) & (r < 25e3)
        assert sq.blockage[east, behind].mean() > 0.5
        assert sq.blockage[east, before].mean() < 0.05
        assert (sq.flags[east, behind] & qpe.FLAG_BLOCK).all()
        assert sq.quality[east, behind].mean() < 0.5 * sq.quality[east, before].mean()


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class TestProducts:
    @pytest.fixture(scope="class")
    def result(self):
        readers = [make_reader(LAT, LON, _const(10.0), t) for t in _times([0, 5, 10])]
        return acc.accumulate(readers, {"min_dbz": -30.0}, resolution_m=2000.0, max_range_km=100.0)

    def test_netcdf_is_cf_labelled_and_round_trips(self, result, tmp_path):
        from engine.hydro import products

        path = products.write_netcdf(result, tmp_path / "qpe.nc")
        ds = xr.open_dataset(path, decode_cf=True)
        assert ds.attrs["Conventions"] == "CF-1.10"
        assert ds.attrs["provenance_type"] == "RETRIEVED"
        a = ds["accumulation"]
        assert a.attrs["units"] == "mm"
        assert a.attrs["standard_name"] == "lwe_thickness_of_precipitation_amount"
        assert a.attrs["provenance_type"] == "RETRIEVED"
        assert a.attrs["cell_methods"] == "time: sum"
        assert a.attrs["grid_mapping"] == "crs"
        assert ds["crs"].attrs["grid_mapping_name"] == "azimuthal_equidistant"
        assert "time_bounds" in ds and ds["time"].attrs["bounds"] == "time_bounds"
        np.testing.assert_allclose(a.values, result.accumulation, equal_nan=True, rtol=1e-6)
        # no field is named or described as an observation
        assert (
            "observ"
            not in " ".join(
                str(v.attrs.get("long_name", "")) for v in ds.data_vars.values()
            ).lower()
        )
        flags = ds["quality_flags"]
        assert "melting_layer_or_ice" in flags.attrs["flag_meanings"]
        assert ds["radar_name"].size == 1

    def test_png_and_map_record(self, result):
        from engine.hydro import products

        out = products.render_png(result)
        assert out["png"][:4] == b"\x89PNG"
        assert out["units"] == "mm"
        rec = products.mosaic_record(result)
        assert "RETRIEVED" in rec.variable
        assert rec.png[:4] == b"\x89PNG"

    def test_figure_says_it_is_an_estimate(self, result):
        from engine.hydro import products

        fig = products.figure(result)
        title = fig.axes[0].get_title()
        assert "RETRIEVED" in title and "not an observation" in title

"""Ensembles of rainfall-estimate errors: known answers, seeds, reliability on a synthetic case."""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest

from engine.hydro import ensemble as he
from engine.hydro import grid as hgrid

CELL = 1000.0


def _result(base, sigma_frac=0.3, duration_s=3600.0, cell_m=CELL):
    """A stand-in of a QpeResult with the fields the ensemble reads."""
    ny, nx = base.shape
    grid = hgrid.make_grid((-88.9, 35.4, -87.1, 36.6), cell_m)
    t0 = np.datetime64("2024-05-01T20:00:00", "ns")
    return SimpleNamespace(
        accumulation=np.asarray(base, dtype=np.float32),
        accumulation_adjusted=None,
        accumulation_sigma=np.asarray(base, dtype=np.float32) * sigma_frac,
        duration_s=duration_s,
        start=t0,
        end=t0 + np.timedelta64(int(duration_s * 1e9), "ns"),
        source_files=["a_V06", "b_V06"],
        basin_series=None,
        grid=SimpleNamespace(
            cell_m=cell_m, x=np.arange(nx) * cell_m, y=np.arange(ny) * cell_m, shape=grid.shape
        ),
    )


MODEL = he.ErrorModel(
    sigma_ln=0.4, shares=(0.3, 0.5, 0.2), corr_length_km=6.0, scale_by_uncertainty=False
)


class TestKnownAnswers:
    def test_conversions(self):
        assert he.sigma_ln_from_cv(0.0) == 0.0
        assert he.sigma_ln_from_cv(1.0) == pytest.approx(math.sqrt(math.log(2.0)))
        # R = a Z^b with b = 1/1.4: 1 dB of Z is b ln(10)/10 = 0.1644 in ln R
        assert he.sigma_ln_from_db(1.0) == pytest.approx(0.1644, abs=2e-4)
        assert he.sigma_ln_from_db(1.0, b=1.0) == pytest.approx(math.log(10) / 10)

    def test_constant_field_mean_and_variance(self):
        # constant 20 mm: E[member] = 20 exactly (mean-preserving) and
        # Var[member] = 20^2 (exp(s^2) - 1) for a lognormal with log-std s = 0.4
        n = 400
        base = np.full((40, 40), 20.0, dtype=np.float32)
        m = he.generate_members(base, CELL, MODEL, n, seed=7)
        s2 = MODEL.sigma_ln**2
        # pool over cells and members; the common part is one draw per member, so the effective
        # number of independent draws is limited: use a loose tolerance for the mean
        assert float(m.mean()) == pytest.approx(20.0, rel=0.03)
        want_var = 20.0**2 * (math.exp(s2) - 1.0)
        assert float(m.var()) == pytest.approx(want_var, rel=0.08)
        # the log of the members has the stated standard deviation (before the mean shift)
        assert float(np.log(m).std()) == pytest.approx(MODEL.sigma_ln, rel=0.04)

    def test_no_rain_stays_no_rain_and_gaps_stay_gaps(self):
        base = np.full((20, 20), 5.0, dtype=np.float32)
        base[:5] = 0.0
        base[5:8] = np.nan
        m = he.generate_members(base, CELL, MODEL, 20, seed=1)
        assert np.all(m[:, :5] == 0.0)
        assert np.all(np.isnan(m[:, 5:8]))
        assert np.all(np.isfinite(m[:, 8:]))

    def test_correlated_field_has_unit_variance_and_the_stated_correlation(self):
        gen = he.CorrelatedField((48, 48), length_cells=6.0)
        rng = np.random.default_rng(3)
        fields = np.stack([gen.sample(rng) for _ in range(300)])
        assert float(fields.var()) == pytest.approx(1.0, rel=0.06)
        # correlation at a lag of one correlation length is exp(-1) for the exponential model
        lag = 6
        c = np.mean(fields[:, :, :-lag] * fields[:, :, lag:])
        assert float(c) == pytest.approx(math.exp(-1.0), abs=0.04)

    def test_larger_correlation_length_gives_a_smoother_error_field(self):
        base = np.full((48, 48), 10.0, dtype=np.float32)
        short = he.ErrorModel(0.4, (0.0, 1.0, 0.0), 2.0, False)
        long = he.ErrorModel(0.4, (0.0, 1.0, 0.0), 12.0, False)
        a = he.generate_members(base, CELL, short, 60, 1)
        b = he.generate_members(base, CELL, long, 60, 1)

        def lag1_corr(x):
            ln = np.log(x)
            ln = ln - ln.mean(axis=0)
            return float(np.mean(ln[:, :, :-1] * ln[:, :, 1:]) / np.mean(ln * ln))

        assert lag1_corr(b) > lag1_corr(a) + 0.2


class TestReproducibility:
    def test_same_seed_same_members_and_first_members_do_not_change(self):
        base = np.full((16, 16), 8.0, dtype=np.float32)
        a = he.generate_members(base, CELL, MODEL, 6, seed=42)
        b = he.generate_members(base, CELL, MODEL, 6, seed=42)
        c = he.generate_members(base, CELL, MODEL, 9, seed=42)
        d = he.generate_members(base, CELL, MODEL, 6, seed=43)
        assert np.array_equal(a, b)
        assert np.array_equal(a, c[:6])
        assert not np.array_equal(a, d)

    def test_run_record_regenerates_the_same_products(self):
        base = np.random.default_rng(0).gamma(2.0, 6.0, (24, 24)).astype(np.float32)
        res = _result(base)
        ens = he.generate(res, MODEL, n_members=12, seed=5, thresholds=(10, 30))
        rec = ens.record()
        assert rec["seed"] == 5 and rec["n_members"] == 12 and rec["type"] == "qpe_ensemble"
        again = he.regenerate(res, rec)
        for k in ens.arrays:
            assert np.array_equal(ens.arrays[k], again.arrays[k], equal_nan=True)
        with pytest.raises(he.EnsembleError, match="run record"):
            he.regenerate(res, {"type": "nope"})


class TestProducts:
    def test_mean_equals_the_estimate_in_expectation_and_probabilities_behave(self):
        base = np.full((30, 30), 20.0, dtype=np.float32)
        res = _result(base)
        ens = he.generate(res, MODEL, n_members=200, seed=2, thresholds=(5, 20, 80))
        assert float(np.nanmean(ens.arrays["ens_mean"])) == pytest.approx(20.0, rel=0.05)
        # probabilities: the median of a mean-preserving lognormal is below the mean, so P(>= 20 mm)
        # is below one half, P(>= 5) is near 1, P(>= 80) is small; they fall with the threshold
        p5, p20, p80 = (float(np.nanmean(ens.arrays[f"prob_ge_{t}mm"])) for t in (5, 20, 80))
        assert p5 > 0.97 and p80 < 0.05 and p5 > p20 > p80
        want20 = 0.5 * math.erfc(
            ((math.log(20.0) - (math.log(20.0) - MODEL.sigma_ln**2 / 2)) / MODEL.sigma_ln)
            / math.sqrt(2)
        )
        assert p20 == pytest.approx(want20, abs=0.03)
        # quantiles are ordered
        assert np.all(ens.arrays["ens_p05"] <= ens.arrays["ens_p50"])
        assert np.all(ens.arrays["ens_p50"] <= ens.arrays["ens_p95"])

    def test_the_mean_is_a_mean_of_depths_not_of_decibels(self):
        # the project rule: depths and rates average linearly. An ensemble of two equally likely
        # values 10 and 40 mm has mean 25 mm; the "mean" of their logarithms would be 20 mm
        vals = np.array([10.0, 40.0])
        assert vals.mean() == 25.0
        assert math.exp(np.log(vals).mean()) == pytest.approx(20.0)
        base = np.full((20, 20), 20.0, dtype=np.float32)
        ens = he.generate(_result(base), MODEL, n_members=300, seed=9)
        mean_of_depth = float(np.nanmean(ens.arrays["ens_mean"]))
        geometric = float(np.exp(np.nanmean(np.log(ens.members))))
        assert mean_of_depth == pytest.approx(20.0, rel=0.05)
        assert geometric < mean_of_depth * 0.95  # the geometric mean is lower by exp(-s^2/2)

    def test_one_volume_is_refused(self):
        res = _result(np.full((8, 8), 5.0), duration_s=0.0)
        with pytest.raises(he.EnsembleError, match="several volumes"):
            he.generate(res)

    def test_option_errors_say_what_is_allowed(self):
        res = _result(np.full((8, 8), 5.0))
        with pytest.raises(he.EnsembleError, match="2 to 500"):
            he.generate(res, MODEL, n_members=1)
        with pytest.raises(he.EnsembleError, match="thresholds"):
            he.generate(res, MODEL, thresholds=(-1,))
        with pytest.raises(he.EnsembleError, match="add up to 1"):
            he.ErrorModel(0.3, (0.5, 0.5, 0.5), 10.0).validate()
        with pytest.raises(he.EnsembleError, match="in \\(0, 3\\]"):
            he.ErrorModel(0.0, (0.3, 0.5, 0.2), 10.0).validate()

    def test_memory_limit_is_a_clear_error(self, monkeypatch):
        monkeypatch.setattr(he, "MAX_MEMBER_BYTES", 1000)
        with pytest.raises(he.EnsembleError, match="fewer members or a coarser grid"):
            he.generate_members(np.ones((20, 20), np.float32), CELL, MODEL, 5, 0)

    def test_default_model_marks_assumptions_and_uses_the_uncertainty_field(self):
        base = np.full((10, 10), 20.0)
        res = _result(base, sigma_frac=0.5)
        m = he.default_model(res)
        assert m.sigma_ln == pytest.approx(he.sigma_ln_from_cv(0.5), rel=1e-6)
        assert "engine's own uncertainty" in m.sources["sigma_ln"]
        assert "ASSUMED DEFAULT" in m.sources["corr_length_km"]
        assert "ASSUMED DEFAULT" in m.sources["shares"]
        m2 = he.default_model(res, sigma_ln=0.2, corr_length_km=10.0, shares=(0.0, 1.0, 0.0))
        assert m2.sources["sigma_ln"] == "given by the user" and m2.corr_length_km == 10.0

    def test_cell_scaling_follows_the_uncertainty_field(self):
        base = np.full((40, 40), 20.0, dtype=np.float32)
        res = _result(base)
        sig = np.full((40, 40), 20.0 * 0.2, dtype=np.float32)
        sig[:, 20:] = 20.0 * 0.8  # four times the relative uncertainty on the right
        res.accumulation_sigma = sig
        w, _ = he._weights(res, "accumulation", True)
        assert w[0, 30] > 2.0 * w[0, 5]
        model = he.ErrorModel(0.4, (0.0, 0.0, 1.0), 5.0, True)
        m = he.generate_members(base, CELL, model, 300, 4, w)
        left, right = np.log(m[:, :, :20]).std(), np.log(m[:, :, 20:]).std()
        assert right > 2.0 * left


class TestGaugeModel:
    @staticmethod
    def _pairs(n, sigma, bias=1.0, seed=0, corr=False):
        rng = np.random.default_rng(seed)
        x = rng.uniform(0, 100e3, n)
        y = rng.uniform(0, 100e3, n)
        radar = rng.uniform(5, 60, n)
        err = rng.standard_normal(n) * sigma
        gauge = radar * bias * np.exp(err)
        return SimpleNamespace(gauge=gauge, radar=radar, x=x, y=y)

    def test_sigma_is_the_rms_of_the_log_ratio_and_bias_is_reported(self):
        res = _result(np.full((10, 10), 20.0))
        p = self._pairs(400, 0.35, bias=1.0, seed=1)
        m = he.model_from_gauges(res, p)
        assert m.sigma_ln == pytest.approx(0.35, rel=0.08)
        assert m.gauge_stats["used"] and m.gauge_stats["n_pairs"] == 400
        assert "gauges: root mean square" in m.sources["sigma_ln"]
        assert abs(m.gauge_stats["mean_ln_ratio"]) < 0.05
        # a 30 % bias is part of the error and is flagged, not silently removed
        pb = self._pairs(400, 0.2, bias=1.3, seed=2)
        mb = he.model_from_gauges(res, pb)
        assert mb.sigma_ln == pytest.approx(math.hypot(0.2, math.log(1.3)), rel=0.08)
        assert mb.shares[0] == pytest.approx(math.log(1.3) ** 2 / mb.sigma_ln**2, rel=0.1)
        assert any("adjust to the gauges first" in n for n in mb.notes)

    def test_too_few_gauges_fall_back_and_say_so(self):
        res = _result(np.full((10, 10), 20.0))
        m = he.model_from_gauges(res, self._pairs(3, 0.3))
        assert m.gauge_stats["used"] is False
        assert any("NOT taken from gauges" in n for n in m.notes)
        assert "ASSUMED DEFAULT" in m.sources["corr_length_km"]

    def test_few_gauges_assume_the_split_and_say_so(self):
        res = _result(np.full((10, 10), 20.0))
        m = he.model_from_gauges(res, self._pairs(6, 0.3))
        assert "ASSUMED" in m.sources["shares"]
        assert sum(m.shares) == pytest.approx(1.0)


class TestReliability:
    def test_perfect_probabilities_give_unit_slope_and_skill(self):
        rng = np.random.default_rng(0)
        p = rng.uniform(0, 1, 20000)
        ev = (rng.uniform(0, 1, p.size) < p).astype(float)
        r = he.reliability(p, ev)
        assert r["reliability_slope"] == pytest.approx(1.0, abs=0.06)
        assert r["brier_skill_score"] > 0.15
        # the same probabilities squashed towards 0.5 are under-confident: slope above 1
        r2 = he.reliability(0.5 + 0.4 * (p - 0.5), ev)
        assert r2["reliability_slope"] > 1.5
        # and over-confident probabilities have a slope below 1
        r3 = he.reliability(np.clip(0.5 + 2.0 * (p - 0.5), 0, 1), ev)
        assert r3["reliability_slope"] < 0.7

    def test_the_synthetic_case_is_reliable_when_the_model_is_right(self):
        """Truth is known by construction: radar = truth / E with E a lognormal field of the stated
        model; gauges read the truth. The ensemble built around the radar with the true model gives
        exceedance probabilities that are reliable at the gauges (slope near 1, positive skill)."""
        ny = nx = 60
        yy, xx = np.mgrid[0:ny, 0:nx]
        truth = (25.0 + 20.0 * np.sin(xx / 9.0) * np.cos(yy / 11.0)).astype(np.float64)
        truth = np.clip(truth, 2.0, None)
        rng = np.random.default_rng(11)
        true_model = he.ErrorModel(0.35, (0.0, 0.8, 0.2), 8.0, False)
        e = he.generate_members(np.ones((ny, nx), np.float32), CELL, true_model, 2, seed=123)[0]
        # the error field of this one realisation is E; the radar sees truth / E
        radar = (truth / e).astype(np.float32)
        res = _result(radar)
        # many gauges at random cells, each reading the truth at its own cell
        n = 600
        gi, gj = rng.integers(2, nx - 2, n), rng.integers(2, ny - 2, n)
        gauge = truth[gj, gi]
        pairs = SimpleNamespace(
            gauge=gauge,
            radar=radar[gj, gi],
            x=gi * CELL,
            y=gj * CELL,
        )
        model = he.model_from_gauges(res, pairs, scale_by_uncertainty=False)
        # the fitted spread is near the true one (a single realisation: loose)
        assert model.sigma_ln == pytest.approx(0.35, rel=0.25)
        ens = he.generate(res, model, n_members=100, seed=3, thresholds=(15, 25, 35))
        # point-sample the members at the gauge cells (radius 0: the gauge is one cell)
        vals = he.sample_members(ens.members, res.grid, pairs.x, pairs.y, radius_cells=0)
        probs, events = [], []
        for t in ens.thresholds:
            probs.append(np.mean(vals >= t, axis=0))
            events.append((gauge >= t).astype(float))
        r = he.reliability(np.concatenate(probs), np.concatenate(events))
        assert r["brier_skill_score"] > 0.2
        assert 0.7 < r["reliability_slope"] < 1.3

    def test_verify_says_not_validated_with_few_gauges(self):
        res = _result(np.full((12, 12), 20.0))
        ens = he.generate(res, MODEL, n_members=10, seed=0)
        p = SimpleNamespace(
            gauge=np.full(5, 20.0), radar=np.full(5, 20.0), x=np.arange(5) * CELL, y=np.zeros(5)
        )
        out = he.verify_with_gauges(ens, res, p)
        assert out["validated"] is False and "NOT validated" in out["note"]


class TestRadarxParity:
    """The algorithms live in radarx; the built-in copies are a transitional fallback.

    Each test runs the call with radarx and with the fallback and compares. Skipped when the
    installed radarx lacks the function.
    """

    @staticmethod
    def _need(name):
        if he._radarx(name) is None:
            pytest.skip(f"the installed radarx has no {name} yet")

    def test_members_agree(self, monkeypatch):
        self._need("qpe_error_ensemble")
        base = np.random.default_rng(0).gamma(2.0, 6.0, (24, 24)).astype(np.float32)
        w = np.random.default_rng(1).uniform(0.5, 2.0, (24, 24)).astype(np.float32)
        a = he.generate_members(base, CELL, MODEL, 8, seed=11, weights=w)
        monkeypatch.setattr(he, "_radarx", lambda name: None)
        b = he.generate_members(base, CELL, MODEL, 8, seed=11, weights=w)
        assert np.allclose(a, b, rtol=2e-4, atol=1e-4)

    def test_error_model_agrees(self, monkeypatch):
        self._need("qpe_error_model")
        res = _result(np.full((10, 10), 20.0))
        pairs = TestGaugeModel._pairs(300, 0.3, bias=1.2, seed=4)
        a = he.model_from_gauges(res, pairs)
        monkeypatch.setattr(he, "_radarx", lambda name: None)
        b = he.model_from_gauges(res, pairs)
        assert a.sigma_ln == pytest.approx(b.sigma_ln, rel=1e-5)
        assert a.shares[0] == pytest.approx(b.shares[0], rel=1e-4)
        assert sum(a.shares) == pytest.approx(1.0, abs=1e-5)

    def test_probabilities_and_reliability_agree(self, monkeypatch):
        self._need("exceedance_probability")
        self._need("reliability")
        self._need("brier_score")
        members = np.random.default_rng(2).gamma(3.0, 5.0, (40, 6, 7)).astype(np.float32)
        members[:, 0, 0] = np.nan
        pa = he._exceedance(members, 15.0)
        rng = np.random.default_rng(3)
        p = rng.uniform(0, 1, 5000)
        ev = (rng.uniform(0, 1, p.size) < p).astype(float)
        ra = he.reliability(p, ev)
        monkeypatch.setattr(he, "_radarx", lambda name: None)
        pb = he._exceedance(members, 15.0)
        rb = he.reliability(p, ev)
        assert np.allclose(pa, pb, equal_nan=True)
        assert ra["brier_score"] == pytest.approx(rb["brier_score"], rel=1e-6)
        assert ra["reliability_slope"] == pytest.approx(rb["reliability_slope"], rel=1e-4)
        assert [b["n"] for b in ra["bins"]] == [b["n"] for b in rb["bins"]]

    def test_uses_radarx_says_which(self):
        u = he.uses_radarx()
        assert "qpe_error_ensemble" in u and all(isinstance(v, bool) for v in u.values())

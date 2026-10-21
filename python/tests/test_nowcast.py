"""Known-answer tests of the nowcast engine (docs/SCIENCE.md 2.5: nothing here comes from running
the feature and calling the output right)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from engine import layer_provenance
from engine.nowcast import ensemble as ens
from engine.nowcast import extrapolate as ex
from engine.nowcast import frames as fr
from engine.nowcast import harness
from engine.nowcast import motion as mo
from engine.nowcast import run as nr
from engine.nowcast import verify as vf

CELL = 1000.0
T0 = np.datetime64("2021-12-11T02:00:00", "ns")


def _grid(n: int = 160):
    return np.mgrid[0:n, 0:n]


def blob(cx: float, cy: float, *, amp: float = 50.0, s: float = 10.0, n: int = 160) -> np.ndarray:
    yy, xx = _grid(n)
    return amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * s * s))


def translated_sequence(dx: float, dy: float, n_frames: int = 4, step_s: int = 300):
    """Two Gaussian echoes moving by (dx, dy) cells per frame; NaN below 5 dBZ."""
    frames, times = [], []
    for k in range(n_frames):
        f = blob(50 + dx * k, 60 + dy * k) + blob(100 + dx * k, 110 + dy * k, amp=45, s=7)
        frames.append(np.where(f > 5, f, np.nan).astype(np.float32))
        times.append(T0 + np.timedelta64(step_s * k, "s"))
    return np.stack(frames), np.asarray(times)


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------


def test_translation_is_recovered_by_lucas_kanade_and_by_cross_correlation() -> None:
    """A field shifted by 2.5 cells east and 1 cell north per 300 s is 8.33 / 3.33 m/s."""
    data, times = translated_sequence(2.5, 1.0)
    m = mo.estimate_motion(data, times, CELL)
    w = np.isfinite(data[-1])
    u, v = m.mean_uv(w)
    assert u == pytest.approx(2.5 * CELL / 300.0, abs=0.15)
    assert v == pytest.approx(1.0 * CELL / 300.0, abs=0.15)
    assert m.method == "lucas_kanade"
    assert np.isfinite(m.u).all() and np.isfinite(m.v).all()
    g = mo.estimate_motion(data, times, CELL, method="global")
    assert g.global_uv[0] == pytest.approx(8.333, abs=0.05)
    assert g.global_uv[1] == pytest.approx(3.333, abs=0.05)


def test_integer_shift_cross_correlation_is_exact() -> None:
    a = blob(60, 70)
    b = np.roll(np.roll(a, 3, axis=1), -2, axis=0)
    dx, dy, cc = mo.global_translation(a, b)
    assert dx == pytest.approx(3.0, abs=1e-6)
    assert dy == pytest.approx(-2.0, abs=1e-6)
    assert cc > 0.99


def test_expanding_blob_gives_divergent_flow() -> None:
    """A blob that grows in size has outward motion: the divergence of the flow is positive."""
    a = np.where(blob(80, 80, s=10) > 5, blob(80, 80, s=10), np.nan)
    b = np.where(blob(80, 80, s=13) > 5, blob(80, 80, s=13), np.nan)
    m = mo.estimate_motion([a, b], [T0, T0 + np.timedelta64(300, "s")], CELL)
    yy, xx = _grid()
    rx, ry = xx - 80, yy - 80
    r = np.hypot(rx, ry)
    ring = (r > 8) & (r < 16)  # where the edge moved
    radial = (m.u * rx + m.v * ry) / np.maximum(r, 1e-6)
    assert np.mean(radial[ring]) > 0.5  # m/s outward
    # and the pattern is centred: no net translation
    assert abs(m.mean_uv(ring)[0]) < 1.0 and abs(m.mean_uv(ring)[1]) < 1.0


def test_noise_gives_no_spurious_motion_and_nan_frames_do_not_break_it() -> None:
    rng = np.random.default_rng(1)
    a = rng.uniform(0, 30, (120, 120)).astype(np.float32)
    b = rng.uniform(0, 30, (120, 120)).astype(np.float32)
    a[:40] = np.nan  # a sector that was not observed
    m = mo.estimate_motion([a, b], [T0, T0 + np.timedelta64(300, "s")], CELL)
    assert np.isfinite(m.u).all() and np.isfinite(m.v).all()
    assert np.hypot(*m.global_uv) < 3.0  # m/s: nothing coherent to track


def test_too_little_echo_means_zero_motion_and_says_so() -> None:
    a = np.full((100, 100), np.nan, dtype=np.float32)
    a[50, 50] = 40.0
    m = mo.estimate_motion([a, a], [T0, T0 + np.timedelta64(300, "s")], CELL)
    assert np.all(m.u == 0) and np.all(m.v == 0)
    assert any("too little echo" in n for n in m.notes)


# ---------------------------------------------------------------------------
# Extrapolation
# ---------------------------------------------------------------------------


def test_uniform_motion_moves_the_field_by_exactly_the_displacement() -> None:
    f = blob(40, 50)
    u = np.full(f.shape, 10.0)  # m/s east
    v = np.full(f.shape, 5.0)
    out = ex.extrapolate(f, u, v, CELL, [600.0, 1200.0], floor=0.0)
    # after 600 s: 6 cells east, 3 north
    py, px = np.unravel_index(np.nanargmax(out[0]), f.shape)
    assert (px, py) == (46, 53)
    py, px = np.unravel_index(np.nanargmax(out[1]), f.shape)
    assert (px, py) == (52, 56)
    assert np.nanmax(out[0]) == pytest.approx(50.0, abs=0.5)  # the peak is kept (persistence)


def test_cells_coming_from_outside_the_observed_area_are_not_invented() -> None:
    f = blob(40, 50)
    observed = np.ones(f.shape, bool)
    observed[:, :20] = False  # the western strip was never observed
    u = np.full(f.shape, 10.0)
    v = np.zeros(f.shape)
    out = ex.extrapolate(f, u, v, CELL, [3000.0], floor=0.0, observed=observed)  # 30 cells
    # columns 20..49 now come from the unobserved strip: NaN, never a number
    assert np.isnan(out[0][:, 20:49]).all()
    assert np.isfinite(out[0][:, 60:]).all()


def test_zr_round_trip_and_floor() -> None:
    dbz = np.array([[-5.0, 5.0, 20.0, 45.0, np.nan]], dtype=np.float32)
    rate = ex.dbz_to_rate(dbz, "wsr88d")
    assert rate[0, 0] == 0.0  # below the rain floor
    assert np.isnan(rate[0, 4])
    assert rate[0, 3] == pytest.approx(((10**4.5) / 300.0) ** (1 / 1.4), rel=1e-5)  # 28.5 mm/h
    back = ex.rate_to_dbz(rate, "wsr88d")
    assert back[0, 2] == pytest.approx(20.0, abs=1e-4)
    assert back[0, 3] == pytest.approx(45.0, abs=1e-4)


def test_decay_models_are_named_and_the_default_changes_nothing() -> None:
    v = np.array([1.0, 2.0, np.nan], dtype=np.float32)
    assert np.array_equal(ex.decay_model("persistence").apply(v, 3600.0), v, equal_nan=True)
    d = ex.decay_model("exponential:3600")
    assert d.apply(v, 3600.0)[0] == pytest.approx(np.exp(-1.0))
    assert d.describe()["model"] == "exponential_decay"
    with pytest.raises(ValueError):
        ex.decay_model("magic")


# ---------------------------------------------------------------------------
# The run, the ensemble and the outputs
# ---------------------------------------------------------------------------


def test_nowcast_known_answer_and_record(tmp_path) -> None:
    data, times = translated_sequence(2.5, 1.0)
    seq = fr.from_arrays(data, times, CELL)
    nc = nr.nowcast(seq, lead_min=30, step_min=5, n_members=6, seed=3)
    k = nc.lead_index(30)
    f = nc.deterministic[k]
    py, px = np.unravel_index(np.nanargmax(np.where(np.isfinite(f), f, -1)), f.shape)
    # the larger blob started at (50, 60); after 3 frames + 6 steps it is at (72.5, 69)
    assert abs(px - 72.5) <= 1.0 and abs(py - 69) <= 1.0
    assert nc.n_members == 6
    assert nc.members_rate.shape == (6, 6, 160, 160)
    assert np.array_equal(nc.members_rate[0], nc.rain_rate, equal_nan=True)  # member 0 unperturbed
    for p in nc.probability.values():
        ok = np.isfinite(p)
        assert ok.any() and (p[ok] >= 0).all() and (p[ok] <= 1).all()
    assert np.all(np.diff(nc.position_sigma_km) > 0)  # uncertainty grows with lead
    rec = nc.record()
    assert rec["type"] == "FORECAST"
    assert rec["seed"] == 3
    assert "not an official forecast" in rec["guidance"]
    prov = nc.provenance()
    assert prov["kind"] == "forecast"
    assert "Lead time" in prov["method"]
    assert "not an official forecast" in prov["uncertainty"]
    # the same seed gives the same members, a different one does not
    again = nr.nowcast(seq, lead_min=30, step_min=5, n_members=6, seed=3)
    assert np.array_equal(again.members_rate, nc.members_rate, equal_nan=True)
    other = nr.nowcast(seq, lead_min=30, step_min=5, n_members=6, seed=4)
    assert not np.array_equal(other.members_rate[1], nc.members_rate[1], equal_nan=True)
    # outputs: CF NetCDF with the forecast class, JSON record, figure
    paths = nc.save(tmp_path / "out")
    names = sorted(p.suffix for p in paths)
    assert names == [".json", ".nc", ".png"]
    import xarray as xr

    with xr.open_dataset(next(p for p in paths if p.suffix == ".nc")) as ds:
        assert ds.attrs["data_class"] == "forecast"
        assert "not an official forecast" in ds.attrs["nowcast_guidance"]
        assert ds["rain_rate"].dims == ("lead", "y", "x")
        assert ds["rain_rate_member"].dims == ("member", "lead", "y", "x")
        assert ds["rain_rate"].attrs["units"] == "mm h-1"
        assert ds["prob_dbz_ge_35"].attrs["threshold"] == 35.0
        assert json.loads(ds.attrs["nowcast_record"])["seed"] == 3


def test_eulerian_persistence_is_the_latest_frame_held() -> None:
    data, times = translated_sequence(2.5, 1.0)
    seq = fr.from_arrays(data, times, CELL)
    base = nr.eulerian_persistence(seq, np.array([5.0, 10.0]))
    assert np.array_equal(base[0], data[-1], equal_nan=True)
    assert np.array_equal(base[1], data[-1], equal_nan=True)


def test_invalid_leads_are_refused() -> None:
    data, times = translated_sequence(1.0, 0.0)
    seq = fr.from_arrays(data, times, CELL)
    with pytest.raises(nr.NowcastError):
        nr.nowcast(seq, lead_min=0, step_min=5)
    with pytest.raises(nr.NowcastError):
        nr.nowcast(seq, lead_min=7, step_min=5)
    with pytest.raises(nr.NowcastError):
        nr.nowcast(seq, lead_min=600, step_min=5)


# ---------------------------------------------------------------------------
# Verification scores against hand-computed values
# ---------------------------------------------------------------------------


def test_categorical_scores_hand_computed() -> None:
    f = np.array([[1.0, 1.0, 0.0, 0.0, np.nan]])
    o = np.array([[1.0, 0.0, 1.0, 0.0, 1.0]])
    h, m, fa, cn = vf.contingency(f, o, 0.5)
    assert (h, m, fa, cn) == (1, 1, 1, 1)  # the NaN cell is not scored
    s = vf.categorical_scores(h, m, fa, cn)
    assert s["pod"] == 0.5 and s["far"] == 0.5 and s["csi"] == pytest.approx(1 / 3)
    assert s["bias"] == 1.0
    assert s["ets"] == pytest.approx((1 - 1.0) / (3 - 1.0))  # hits_random = 2*2/4 = 1


def test_fss_is_one_for_a_perfect_forecast_and_grows_with_scale() -> None:
    o = (blob(60, 60) > 20).astype(float)
    f_perfect = o.copy()
    assert vf.fss_from_terms(*vf.fss_terms(f_perfect, o, 0.5, 1)) == pytest.approx(1.0)
    f_shift = np.roll(o, 6, axis=1)  # a 6-cell displacement error
    small = vf.fss_from_terms(*vf.fss_terms(f_shift, o, 0.5, 1))
    large = vf.fss_from_terms(*vf.fss_terms(f_shift, o, 0.5, 17))
    assert small < large < 1.0


def test_brier_reliability_roc_and_crps_hand_computed() -> None:
    p = np.array([[0.0, 1.0, 0.5, 0.5]])
    o = np.array([[0.0, 1.0, 1.0, 0.0]])
    t = vf.prob_terms(p, o, 0.5)
    s = vf.probabilistic_scores(t)
    assert s["brier"] == pytest.approx((0 + 0 + 0.25 + 0.25) / 4)
    assert s["base_rate"] == 0.5
    assert s["roc_area"] == pytest.approx(0.75)  # ranks: one perfect pair, one tie
    bins = [r for r in s["reliability"] if r["n"] > 0]
    assert any(r["forecast_mean"] == 0.5 and r["observed_frequency"] == 0.5 for r in bins)
    # CRPS of a two-member ensemble {0, 2} against 1: E|X-y| = 1, E|X-X'| = 1 -> 0.5
    members = np.array([[[0.0]], [[2.0]]])
    assert vf.crps_ensemble(members, np.array([[1.0]])) == pytest.approx(0.5)
    # a deterministic forecast's CRPS is its absolute error
    assert vf.crps_ensemble(np.array([[[3.0]]]), np.array([[1.0]])) == pytest.approx(2.0)


def test_block_bootstrap_resamples_events_not_frames() -> None:
    recs = []
    for e, val in (("A", 10), ("B", 20), ("C", 30)):
        for k in range(5):  # five dependent forecasts per event
            r = vf.Record(event=e, origin=str(k), model="m", lead_min=5.0)
            r.cont = vf.error_terms(np.array([float(val)]), np.array([0.0]))
            recs.append(r)
    res = vf.block_bootstrap(recs, vf.stat_continuous("mae"), n_boot=300, seed=1)
    assert res["value"] == pytest.approx(20.0)
    assert res["n_events"] == 3
    lo, hi = res["ci"]
    assert lo >= 10.0 and hi <= 30.0 and lo < 20.0 < hi
    # the resamples take whole events: only the three event means (and their mixes) appear
    one = vf.block_bootstrap(recs[:5], vf.stat_continuous("mae"), n_boot=50)
    assert one["ci"] == [None, None]  # a single event: no interval, said plainly


def test_harness_scores_a_translating_sequence_better_than_persistence() -> None:
    data, times = translated_sequence(2.0, 0.5, n_frames=10)
    seq = fr.from_arrays(data, times, CELL)
    recs = harness.verify_sequence(seq, event="synthetic", lead_min=20, step_min=5, n_frames=3)
    summary = harness.summarise(recs, n_boot=0)
    lk = summary["by_model_lead"]["lucas_kanade"][20.0]["categorical"][20.0]["csi"]
    eu = summary["by_model_lead"]["eulerian"][20.0]["categorical"][20.0]["csi"]
    assert lk > 0.9 > eu
    stats = harness.motion_error_samples(seq, lead_min=20, step_min=5)
    assert stats["samples"]
    assert all(abs(s["parallel"]) < 0.5 for s in stats["samples"])  # a steady translation


def test_motion_error_statistics_drive_the_spread() -> None:
    small = ens.MotionErrorStats([5.0, 60.0], [0.5, 0.5], [0.5, 0.5])
    large = ens.MotionErrorStats([5.0, 60.0], [5.0, 5.0], [5.0, 5.0])
    data, times = translated_sequence(2.0, 0.0)
    seq = fr.from_arrays(data, times, CELL)
    a = nr.nowcast(seq, lead_min=30, step_min=5, n_members=8, motion_stats=small)
    b = nr.nowcast(seq, lead_min=30, step_min=5, n_members=8, motion_stats=large)
    assert np.nanmean(b.spread_rate[-1]) > 3 * np.nanmean(a.spread_rate[-1])
    assert b.position_sigma_km[-1] == pytest.approx(5.0 * 1800.0 / 1000.0)


# ---------------------------------------------------------------------------
# Frames and provenance
# ---------------------------------------------------------------------------


def test_frames_round_trip_through_netcdf(tmp_path) -> None:
    data, times = translated_sequence(1.0, 1.0, n_frames=3)
    cov = np.ones(data.shape[1:], bool)
    cov[:10] = False
    seq = fr.from_arrays(data, times, CELL, covered=cov, origin=(36.0, -88.0))
    seq.proj4 = fr.aeqd_proj4(36.0, -88.0)
    p = seq.to_netcdf(tmp_path / "frames.nc")
    back = fr.from_netcdf([p])
    assert back.n == 3 and back.field == "reflectivity" and back.cell_m == CELL
    assert np.array_equal(back.data, seq.data, equal_nan=True)
    assert np.array_equal(back.coverage, cov)
    assert back.origin == (36.0, -88.0)


def test_frames_refuse_bad_input() -> None:
    with pytest.raises(fr.FrameError):
        fr.FrameSequence(
            times=np.array([T0, T0]),
            data=np.zeros((2, 4, 4)),
            field="reflectivity",
            cell_m=1.0,
            x=np.arange(4),
            y=np.arange(4),
        )
    with pytest.raises(fr.FrameError):
        fr.FrameSequence(
            times=np.array([T0]),
            data=np.zeros((1, 4, 4)),
            field="wind",
            cell_m=1.0,
            x=np.arange(4),
            y=np.arange(4),
        )


def test_forecast_provenance_class_and_chip_text() -> None:
    assert "forecast" in layer_provenance.CLASSES
    assert layer_provenance.SHORT["forecast"] == "FCST"
    p = layer_provenance.forecast(
        source="KPAH",
        run_time="2021-12-11T02:30:00Z",
        origin_time="2021-12-11T02:10:40Z",
        valid_end="2021-12-11T03:10:40Z",
        lead_text="5 to 60 min",
        method="Advection.",
        skill="CSI 0.4 at 30 min on the verification set.",
    )
    d = p.to_dict()
    assert d["kind"] == "forecast"
    assert "5 to 60 min" in d["method"]
    assert "CSI 0.4" in d["uncertainty"] and "not an official forecast" in d["uncertainty"]
    attrs = layer_provenance.attrs(d)
    assert attrs["data_class"] == "forecast"
    assert "future" in attrs["data_class_definition"]

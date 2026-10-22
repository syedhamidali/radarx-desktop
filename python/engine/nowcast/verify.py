"""Verification of nowcasts against what was observed later.

Nothing here is tuned: a score is a function of forecast and observation only. The
accumulators keep the **sufficient statistics** of every forecast (contingency counts, squared
error sums, reliability counts ...) so that pooled scores can be recomputed for any subset: that
is what the block bootstrap over events needs (forecasts of one event are dependent; events are
the independent blocks, Wilks 2011 section 5.3.4).

Deterministic scores at thresholds (Wilks 2011, chapter 8; Jolliffe and Stephenson 2012):
POD (hit rate), FAR (false alarm ratio), CSI (threat score), frequency bias, ETS (Gilbert skill
score), and the fractions skill score FSS at several neighbourhood scales (Roberts and Lean
2008); MAE and RMSE of the rain rate; the Pearson correlation.

Probabilistic scores: Brier score and its skill versus the sample climatology (Wilks 2011,
8.4.2), reliability-diagram counts, the area under the ROC curve (trapezoid over the probability
bins) and the CRPS of an ensemble (Hersbach 2000, the formula of Gneiting and Raftery 2007
``E|X - y| - 0.5 E|X - X'|``, evaluated exactly over the members).

References
----------
* Gneiting, T., and A. E. Raftery, 2007: Strictly proper scoring rules, prediction, and
  estimation. *J. Amer. Stat. Assoc.*, **102**, 359-378.
* Hersbach, H., 2000: Decomposition of the continuous ranked probability score for ensemble
  prediction systems. *Wea. Forecasting*, **15**, 559-570.
* Jolliffe, I. T., and D. B. Stephenson (eds.), 2012: *Forecast Verification: A Practitioner's
  Guide in Atmospheric Science*, 2nd ed. Wiley.
* Roberts, N. M., and H. W. Lean, 2008: Scale-selective verification of rainfall accumulations
  from high-resolution forecasts of convective events. *Mon. Wea. Rev.*, **136**, 78-97.
* Wilks, D. S., 2011: *Statistical Methods in the Atmospheric Sciences*, 3rd ed. Academic Press.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from engine._lazy import LazyModule

ndimage = LazyModule("scipy.ndimage")

DEFAULT_DBZ_THRESHOLDS = (20.0, 35.0, 45.0)
DEFAULT_RATE_THRESHOLDS = (0.5, 2.0, 10.0)
DEFAULT_FSS_SCALES_CELLS = (1, 3, 5, 9, 17, 33)
PROB_BINS = np.linspace(0.0, 1.0, 11)  # reliability / ROC bins


# ---------------------------------------------------------------------------
# One forecast against one observation
# ---------------------------------------------------------------------------


def common_mask(forecast: np.ndarray, observed: np.ndarray) -> np.ndarray:
    """Cells where both are known (a forecast of an unobserved cell is not scored)."""
    return np.isfinite(forecast) & np.isfinite(observed)


def contingency(
    forecast: np.ndarray, observed: np.ndarray, threshold: float, mask: np.ndarray | None = None
) -> tuple[int, int, int, int]:
    """``(hits, misses, false_alarms, correct_negatives)`` for ``>= threshold``."""
    m = common_mask(forecast, observed) if mask is None else mask
    f = (forecast >= threshold) & m
    o = (observed >= threshold) & m
    hits = int((f & o).sum())
    misses = int((~f & o).sum())
    fa = int((f & ~o).sum())
    cn = int((~f & ~o & m).sum())
    return hits, misses, fa, cn


def categorical_scores(hits: float, misses: float, fa: float, cn: float) -> dict[str, float | None]:
    """POD, FAR, CSI, bias and ETS from pooled counts (``None`` where undefined)."""
    n = hits + misses + fa + cn

    def div(a: float, b: float) -> float | None:
        return float(a / b) if b > 0 else None

    hits_random = (hits + misses) * (hits + fa) / n if n > 0 else 0.0
    ets_den = hits + misses + fa - hits_random
    return {
        "pod": div(hits, hits + misses),
        "far": div(fa, hits + fa),
        "csi": div(hits, hits + misses + fa),
        "bias": div(hits + fa, hits + misses),
        "ets": div(hits - hits_random, ets_den) if ets_den > 0 else None,
        "base_rate": div(hits + misses, n),
        "n": float(n),
    }


def fractions(binary: np.ndarray, valid: np.ndarray, scale: int) -> np.ndarray:
    """Neighbourhood fraction of ``binary`` over ``scale x scale`` cells, counting only valid
    cells (a window at the edge of the data is normalised by the valid cells it holds)."""
    if scale <= 1:
        return binary.astype(np.float64)
    num = ndimage.uniform_filter(binary.astype(np.float64), scale, mode="constant", cval=0.0)
    den = ndimage.uniform_filter(valid.astype(np.float64), scale, mode="constant", cval=0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, 0.0)


def fss_terms(
    forecast: np.ndarray,
    observed: np.ndarray,
    threshold: float,
    scale: int,
    mask: np.ndarray | None = None,
) -> tuple[float, float]:
    """``(numerator, denominator)`` of ``FSS = 1 - num / den`` so sums can be pooled."""
    m = common_mask(forecast, observed) if mask is None else mask
    f = fractions((forecast >= threshold) & m, m, scale)
    o = fractions((observed >= threshold) & m, m, scale)
    num = float(((f - o) ** 2)[m].sum())
    den = float((f**2)[m].sum() + (o**2)[m].sum())
    return num, den


def fss_from_terms(num: float, den: float) -> float | None:
    return float(1.0 - num / den) if den > 0 else None


def error_terms(
    forecast: np.ndarray, observed: np.ndarray, mask: np.ndarray | None = None
) -> dict[str, float]:
    """Sums for MAE / RMSE / correlation over the common cells (continuous values)."""
    m = common_mask(forecast, observed) if mask is None else mask
    f = forecast[m].astype(np.float64)
    o = observed[m].astype(np.float64)
    return {
        "n": float(f.size),
        "abs": float(np.abs(f - o).sum()),
        "sq": float(((f - o) ** 2).sum()),
        "f": float(f.sum()),
        "o": float(o.sum()),
        "ff": float((f * f).sum()),
        "oo": float((o * o).sum()),
        "fo": float((f * o).sum()),
    }


def continuous_scores(t: dict[str, float]) -> dict[str, float | None]:
    n = t["n"]
    if n <= 0:
        return {"mae": None, "rmse": None, "bias": None, "corr": None, "n": 0.0}
    mf, mo = t["f"] / n, t["o"] / n
    vf = t["ff"] / n - mf * mf
    vo = t["oo"] / n - mo * mo
    cov = t["fo"] / n - mf * mo
    corr = cov / math.sqrt(vf * vo) if vf > 0 and vo > 0 else None
    return {
        "mae": t["abs"] / n,
        "rmse": math.sqrt(t["sq"] / n),
        "bias": mf - mo,
        "corr": corr,
        "n": n,
    }


# ---------------------------------------------------------------------------
# Probabilistic
# ---------------------------------------------------------------------------


def prob_terms(
    prob: np.ndarray, observed: np.ndarray, threshold: float, mask: np.ndarray | None = None
) -> dict[str, Any]:
    """Brier sums, reliability counts and ROC counts of one probability field."""
    m = (np.isfinite(prob) & np.isfinite(observed)) if mask is None else mask
    p = np.clip(prob[m].astype(np.float64), 0.0, 1.0)
    o = (observed[m] >= threshold).astype(np.float64)
    n = p.size
    bins = np.clip(np.digitize(p, PROB_BINS[1:-1]), 0, PROB_BINS.size - 2)
    nb = PROB_BINS.size - 1
    count = np.bincount(bins, minlength=nb).astype(np.float64)
    psum = np.bincount(bins, weights=p, minlength=nb)
    osum = np.bincount(bins, weights=o, minlength=nb)
    # ROC: for each decision threshold q, hits = sum(o[p >= q]), false alarms = sum((1-o)[p >= q])
    qs = PROB_BINS[1:-1]
    roc_h = np.array([float(o[p >= q].sum()) for q in qs])
    roc_f = np.array([float((1.0 - o)[p >= q].sum()) for q in qs])
    return {
        "n": float(n),
        "brier": float(((p - o) ** 2).sum()),
        "events": float(o.sum()),
        "rel_count": count,
        "rel_psum": psum,
        "rel_osum": osum,
        "roc_hits": roc_h,
        "roc_fa": roc_f,
    }


def probabilistic_scores(t: dict[str, Any]) -> dict[str, Any]:
    """Brier score, BSS versus the sample climatology, reliability table and ROC area."""
    n = t["n"]
    if n <= 0:
        return {"brier": None, "bss_climatology": None, "roc_area": None, "reliability": []}
    base = t["events"] / n
    bs = t["brier"] / n
    bs_clim = base * (1.0 - base)
    bss = 1.0 - bs / bs_clim if bs_clim > 0 else None
    events, non = t["events"], n - t["events"]
    hr = np.concatenate([[1.0], t["roc_hits"] / events if events > 0 else t["roc_hits"] * 0, [0.0]])
    far = np.concatenate([[1.0], t["roc_fa"] / non if non > 0 else t["roc_fa"] * 0, [0.0]])
    order = np.argsort(far)
    area = float(np.trapezoid(hr[order], far[order])) if events > 0 and non > 0 else None
    rel = []
    for k in range(t["rel_count"].size):
        c = t["rel_count"][k]
        rel.append(
            {
                "bin": [float(PROB_BINS[k]), float(PROB_BINS[k + 1])],
                "n": float(c),
                "forecast_mean": float(t["rel_psum"][k] / c) if c > 0 else None,
                "observed_frequency": float(t["rel_osum"][k] / c) if c > 0 else None,
            }
        )
    return {
        "brier": bs,
        "bss_climatology": bss,
        "base_rate": base,
        "roc_area": area,
        "reliability": rel,
        "n": n,
    }


def crps_ensemble(
    members: np.ndarray, observed: np.ndarray, mask: np.ndarray | None = None
) -> float:
    """Sum over the common cells of the ensemble CRPS (``members``: ``(n_member, ny, nx)``)."""
    m = (np.isfinite(members).all(axis=0) & np.isfinite(observed)) if mask is None else mask
    x = members[:, m].astype(np.float64)  # (M, n)
    y = observed[m].astype(np.float64)
    if x.size == 0:
        return 0.0
    term1 = np.abs(x - y[None, :]).mean(axis=0)
    xs = np.sort(x, axis=0)
    mm = xs.shape[0]
    # E|X - X'| = 2/M^2 * sum_i (2i - M - 1) x_(i)  (sorted members; Gneiting & Raftery 2007)
    coef = (2.0 * np.arange(1, mm + 1) - mm - 1.0) / (mm * mm)
    term2 = (coef[:, None] * xs).sum(axis=0)
    return float((term1 - term2).sum())


# ---------------------------------------------------------------------------
# Records and pooling
# ---------------------------------------------------------------------------


@dataclass
class Record:
    """Sufficient statistics of one forecast (one model, one origin, one lead)."""

    event: str
    origin: str
    model: str
    lead_min: float
    cat: dict[float, tuple[int, int, int, int]] = field(default_factory=dict)
    fss: dict[tuple[float, int], tuple[float, float]] = field(default_factory=dict)
    cont: dict[str, float] = field(default_factory=dict)  # rain rate error sums
    prob: dict[float, dict[str, Any]] = field(default_factory=dict)
    crps_sum: float | None = None
    crps_n: float = 0.0
    mae_det_sum: float | None = None  # |deterministic - obs| over the CRPS cells (comparison)
    spread_sum: float | None = None  # ensemble std over the CRPS cells
    motion_error: tuple[float, float] | None = None  # (parallel, perpendicular) m/s


def score_forecast(
    *,
    event: str,
    origin: str,
    model: str,
    lead_min: float,
    forecast: np.ndarray,
    observed: np.ndarray,
    thresholds: Sequence[float],
    fss_scales: Sequence[int],
    rate_forecast: np.ndarray | None = None,
    rate_observed: np.ndarray | None = None,
    prob: dict[float, np.ndarray] | None = None,
    members_rate: np.ndarray | None = None,
) -> Record:
    """Score one forecast field (and its probabilities / ensemble) against the observation."""
    rec = Record(event=event, origin=origin, model=model, lead_min=float(lead_min))
    m = common_mask(forecast, observed)
    for thr in thresholds:
        rec.cat[float(thr)] = contingency(forecast, observed, thr, m)
        for s in fss_scales:
            rec.fss[(float(thr), int(s))] = fss_terms(forecast, observed, thr, s, m)
    if rate_forecast is not None and rate_observed is not None:
        rec.cont = error_terms(
            rate_forecast, rate_observed, common_mask(rate_forecast, rate_observed)
        )
    if prob:
        for thr, p in prob.items():
            rec.prob[float(thr)] = prob_terms(p, observed, thr)
    if members_rate is not None and rate_observed is not None:
        mm = np.isfinite(members_rate).all(axis=0) & np.isfinite(rate_observed)
        rec.crps_sum = crps_ensemble(members_rate, rate_observed, mm)
        rec.crps_n = float(mm.sum())
        if rate_forecast is not None:
            rec.mae_det_sum = float(np.abs(rate_forecast[mm] - rate_observed[mm]).sum())
        rec.spread_sum = float(members_rate[:, mm].std(axis=0).sum())
    return rec


def pooled_scores(records: Iterable[Record]) -> dict[str, Any]:
    """Pooled scores of a set of records (same model and lead, usually), keyed by threshold."""
    recs = list(records)
    out: dict[str, Any] = {"n_forecasts": len(recs), "categorical": {}, "fss": {}, "prob": {}}
    thr_all = sorted({t for r in recs for t in r.cat})
    for thr in thr_all:
        h = m = fa = cn = 0
        for r in recs:
            if thr in r.cat:
                a, b, c, d = r.cat[thr]
                h, m, fa, cn = h + a, m + b, fa + c, cn + d
        out["categorical"][thr] = categorical_scores(h, m, fa, cn)
    keys = sorted({k for r in recs for k in r.fss})
    for thr, s in keys:
        num = sum(r.fss[(thr, s)][0] for r in recs if (thr, s) in r.fss)
        den = sum(r.fss[(thr, s)][1] for r in recs if (thr, s) in r.fss)
        out["fss"].setdefault(thr, {})[s] = fss_from_terms(num, den)
    cont = dict.fromkeys(("n", "abs", "sq", "f", "o", "ff", "oo", "fo"), 0.0)
    for r in recs:
        for k in cont:
            cont[k] += r.cont.get(k, 0.0)
    out["continuous"] = continuous_scores(cont)
    pthr = sorted({t for r in recs for t in r.prob})
    for thr in pthr:
        acc: dict[str, Any] | None = None
        for r in recs:
            t = r.prob.get(thr)
            if t is None:
                continue
            if acc is None:
                acc = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in t.items()}
            else:
                for k, v in t.items():
                    acc[k] = acc[k] + v
        if acc is not None:
            out["prob"][thr] = probabilistic_scores(acc)
    cs = [r for r in recs if r.crps_sum is not None and r.crps_n > 0]
    if cs:
        n = sum(r.crps_n for r in cs)
        out["crps"] = sum(r.crps_sum for r in cs) / n
        out["crps_mae_deterministic"] = (
            sum(r.mae_det_sum for r in cs if r.mae_det_sum is not None) / n
        )
        out["spread_mean"] = sum(r.spread_sum for r in cs if r.spread_sum is not None) / n
    return out


# ---------------------------------------------------------------------------
# Block bootstrap over events
# ---------------------------------------------------------------------------


def block_bootstrap(
    records: Sequence[Record],
    statistic: Callable[[list[Record]], float | None],
    *,
    n_boot: int = 1000,
    seed: int = 0,
    level: float = 0.95,
) -> dict[str, Any]:
    """Confidence interval of ``statistic(records)`` from resampling **events** with
    replacement (all forecasts of an event stay together). Returns the point estimate, the
    interval, the number of events and of successful resamples."""
    events = sorted({r.event for r in records})
    by_event = {e: [r for r in records if r.event == e] for e in events}
    point = statistic(list(records))
    if len(events) < 2 or point is None:
        return {"value": point, "ci": [None, None], "n_events": len(events), "n_boot": 0}
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(events), size=len(events))
        sample = [r for i in pick for r in by_event[events[i]]]
        v = statistic(sample)
        if v is not None and math.isfinite(v):
            vals.append(v)
    if len(vals) < 10:
        return {"value": point, "ci": [None, None], "n_events": len(events), "n_boot": len(vals)}
    lo, hi = np.percentile(vals, [50 * (1 - level), 50 * (1 + level)])
    return {
        "value": float(point),
        "ci": [float(lo), float(hi)],
        "n_events": len(events),
        "n_boot": len(vals),
    }


def stat_categorical(name: str, thr: float) -> Callable[[list[Record]], float | None]:
    def f(recs: list[Record]) -> float | None:
        return pooled_scores(recs)["categorical"].get(thr, {}).get(name)

    return f


def stat_fss(thr: float, scale: int) -> Callable[[list[Record]], float | None]:
    def f(recs: list[Record]) -> float | None:
        return pooled_scores(recs)["fss"].get(thr, {}).get(scale)

    return f


def stat_continuous(name: str) -> Callable[[list[Record]], float | None]:
    def f(recs: list[Record]) -> float | None:
        return pooled_scores(recs)["continuous"].get(name)

    return f


def stat_prob(name: str, thr: float) -> Callable[[list[Record]], float | None]:
    def f(recs: list[Record]) -> float | None:
        return pooled_scores(recs)["prob"].get(thr, {}).get(name)

    return f


def stat_crps() -> Callable[[list[Record]], float | None]:
    def f(recs: list[Record]) -> float | None:
        return pooled_scores(recs).get("crps")

    return f


def skill_difference(
    records: Sequence[Record],
    model: str,
    reference: str,
    statistic: Callable[[list[Record]], float | None],
    *,
    n_boot: int = 1000,
    seed: int = 0,
) -> dict[str, Any]:
    """``statistic(model) - statistic(reference)`` with a paired block-bootstrap interval: the
    same resampled events are used for both, so the difference is tested, not two intervals."""

    def diff(recs: list[Record]) -> float | None:
        a = statistic([r for r in recs if r.model == model])
        b = statistic([r for r in recs if r.model == reference])
        return None if a is None or b is None else a - b

    return block_bootstrap(list(records), diff, n_boot=n_boot, seed=seed)

"""The verification harness: forecasts from the frames up to ``t``, scored against the frames
observed at ``t + lead``.

For every usable origin of a sequence (enough frames before it for the motion, at least one
frame after it) the harness makes the nowcast of each **model** and scores it at every lead for
which an observed frame exists (within half a step of the valid time):

``lucas_kanade``
    the dense optical-flow nowcast (the method);
``global``
    one translation vector for the whole domain (the fallback / the classic single storm motion);
``eulerian``
    Eulerian persistence: the latest frame held still. **Every** skill statement is relative to
    this baseline.

The records (:class:`engine.nowcast.verify.Record`) are the sufficient statistics; nothing is
tuned here. :func:`motion_error_samples` measures, on tuning sequences only, the error of the
motion used at ``t`` against the motion the echo really had over the following lead (the mean
of the consecutive-pair motions estimated afterwards): the statistics the ensemble perturbs with.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from engine import provenance as prov
from engine.nowcast import ensemble as ens
from engine.nowcast import extrapolate as ex
from engine.nowcast import motion as mo
from engine.nowcast import run as nr
from engine.nowcast import verify as vf
from engine.nowcast.frames import FIELD_REFLECTIVITY, FrameSequence

ProgressFn = Callable[[int, str], None]
MODELS = ("lucas_kanade", "global", "eulerian")


def _observed_at(frames: FrameSequence, when: np.datetime64, tol_s: float) -> int | None:
    d = np.abs((frames.times - when) / np.timedelta64(1, "s"))
    k = int(np.argmin(d))
    return k if d[k] <= tol_s else None


def usable_origins(frames: FrameSequence, n_frames: int, min_future: int = 1) -> list[int]:
    """Frame indices that can serve as a forecast origin."""
    return list(range(max(1, n_frames - 1), frames.n - min_future))


def verify_sequence(
    frames: FrameSequence,
    *,
    event: str,
    lead_min: float = 60.0,
    step_min: float = 5.0,
    n_members: int = 0,
    n_frames: int = 3,
    models: Sequence[str] = MODELS,
    thresholds: Sequence[float] | None = None,
    rate_thresholds: Sequence[float] = vf.DEFAULT_RATE_THRESHOLDS,
    fss_scales_km: Sequence[float] = (1.0, 3.0, 5.0, 9.0, 17.0, 33.0),
    zr_relation: str = "wsr88d",
    motion_stats: ens.MotionErrorStats | None = None,
    origins: Sequence[int] | None = None,
    origin_stride: int = 1,
    seed: int = 0,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[vf.Record]:
    """Score every model at every usable origin and lead; returns the records."""
    report = progress or (lambda _p, _m: None)
    is_refl = frames.field == FIELD_REFLECTIVITY
    thr = tuple(
        float(t)
        for t in (
            thresholds or (vf.DEFAULT_DBZ_THRESHOLDS if is_refl else vf.DEFAULT_RATE_THRESHOLDS)
        )
    )
    scales = tuple(sorted({max(1, round(s * 1000.0 / frames.cell_m) | 1) for s in fss_scales_km}))
    tol = 0.5 * step_min * 60.0
    orgs = (
        list(origins)
        if origins is not None
        else usable_origins(frames, n_frames)[:: max(1, origin_stride)]
    )
    stats = motion_stats or ens.MotionErrorStats.load()
    records: list[vf.Record] = []
    cov = frames.coverage
    for oi, origin in enumerate(orgs):
        if cancel and cancel():
            raise nr.NowcastCancelled("cancelled")
        report(int(100 * oi / max(len(orgs), 1)), f"{event}: origin {oi + 1}/{len(orgs)}")
        t0 = frames.times[origin]
        origin_iso = prov.iso_utc(t0)
        leads = nr._leads(lead_min, step_min)
        # which leads have an observation
        obs_idx = {}
        for k, ld in enumerate(leads):
            j = _observed_at(frames, t0 + np.timedelta64(round(ld * 60.0), "s"), tol)
            if j is not None and j > origin:
                obs_idx[k] = j
        if not obs_idx:
            continue
        for model in models:
            if model == "eulerian":
                det = nr.eulerian_persistence(frames, leads, origin)
                det_rate = (
                    ex.dbz_to_rate(det, zr_relation)
                    if is_refl
                    else np.where(np.isfinite(det), np.maximum(det, 0), np.nan).astype(np.float32)
                )
                nc = None
            else:
                nc = nr.nowcast(
                    frames,
                    lead_min=lead_min,
                    step_min=step_min,
                    n_members=n_members if model == "lucas_kanade" else 0,
                    motion_method=model,
                    n_frames=n_frames,
                    origin_index=origin,
                    thresholds=thr,
                    rate_thresholds=rate_thresholds,
                    zr_relation=zr_relation,
                    motion_stats=stats,
                    seed=seed,
                    keep_members=True,
                )
                det, det_rate = nc.deterministic, nc.rain_rate
            for k, j in obs_idx.items():
                obs = frames.data[j].copy()
                obs[~cov] = np.nan
                obs_rate = (
                    ex.dbz_to_rate(obs, zr_relation)
                    if is_refl
                    else np.where(np.isfinite(obs), np.maximum(obs, 0), np.nan).astype(np.float32)
                )
                prob = None
                members = None
                if nc is not None:
                    prob = {t: nc.probability[t][k] for t in thr if t in nc.probability}
                    if nc.members_rate is not None and nc.n_members > 1:
                        members = nc.members_rate[:, k]
                elif model == "eulerian":
                    # the baseline's "probability": the deterministic field as 0/1
                    prob = {
                        t: np.where(np.isfinite(det[k]), (det[k] >= t).astype(np.float32), np.nan)
                        for t in thr
                    }
                rec = vf.score_forecast(
                    event=event,
                    origin=origin_iso,
                    model=model,
                    lead_min=float(leads[k]),
                    forecast=det[k],
                    observed=obs,
                    thresholds=thr,
                    fss_scales=scales,
                    rate_forecast=det_rate[k],
                    rate_observed=obs_rate,
                    prob=prob,
                    members_rate=members,
                )
                records.append(rec)
    report(100, f"{event}: scored")
    return records


def motion_error_samples(
    frames: FrameSequence,
    *,
    lead_min: float = 60.0,
    step_min: float = 5.0,
    n_frames: int = 3,
    motion_method: str = "lucas_kanade",
    origin_stride: int = 1,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Motion error (parallel, perpendicular; m/s) per origin and lead, on one sequence.

    The "realised" motion over ``[t, t + lead]`` is the echo-weighted mean of the pair motions
    estimated with the frames observed afterwards; the error is the motion used at ``t`` minus
    the realised one, both as echo-weighted domain means.
    """
    report = progress or (lambda _p, _m: None)
    is_refl = frames.field == FIELD_REFLECTIVITY
    floor = mo.DEFAULT_FLOOR_DBZ if is_refl else 0.1
    # pair motions of the whole sequence, once
    pair_uv: list[tuple[float, float] | None] = []
    for k in range(frames.n - 1):
        report(int(50 * k / max(frames.n - 1, 1)), "pair motions")
        try:
            m = mo.estimate_motion(
                frames.data[k : k + 2],
                frames.times[k : k + 2],
                frames.cell_m,
                method=motion_method,
                n_frames=2,
                floor=floor,
            )
            w = np.isfinite(frames.data[k + 1]) & frames.coverage
            pair_uv.append(m.mean_uv(w) if w.sum() > 50 else None)
        except mo.MotionError:
            pair_uv.append(None)
    leads = nr._leads(lead_min, step_min)
    samples: list[dict[str, Any]] = []
    orgs = usable_origins(frames, n_frames)[:: max(1, origin_stride)]
    for oi, origin in enumerate(orgs):
        report(50 + int(50 * oi / max(len(orgs), 1)), "motion errors")
        first = max(0, origin - max(2, n_frames) + 1)
        try:
            used = mo.estimate_motion(
                frames.data[first : origin + 1],
                frames.times[first : origin + 1],
                frames.cell_m,
                method=motion_method,
                n_frames=n_frames,
                floor=floor,
            )
        except mo.MotionError:
            continue
        w = np.isfinite(frames.data[origin]) & frames.coverage
        if w.sum() < 50:
            continue
        u0, v0 = used.mean_uv(w)
        spd = math.hypot(u0, v0)
        e_par = np.array([u0, v0]) / spd if spd > 1e-6 else np.array([1.0, 0.0])
        e_perp = np.array([-e_par[1], e_par[0]])
        t0 = frames.times[origin]
        for ld in leads:
            t_end = t0 + np.timedelta64(round(ld * 60.0), "s")
            # pairs whose later frame lies within (t0, t_end + half step]
            sel = [
                pair_uv[k]
                for k in range(origin, frames.n - 1)
                if frames.times[k + 1] <= t_end + np.timedelta64(int(0.5 * step_min * 60), "s")
            ]
            sel = [p for p in sel if p is not None]
            if not sel:
                continue
            if (frames.times[min(origin + len(sel), frames.n - 1)] - t0) / np.timedelta64(
                1, "s"
            ) < 0.5 * ld * 60.0:
                continue  # the observed frames do not reach this lead
            ru, rv = float(np.mean([p[0] for p in sel])), float(np.mean([p[1] for p in sel]))
            du, dv = u0 - ru, v0 - rv
            samples.append(
                {
                    "origin": prov.iso_utc(t0),
                    "lead_min": float(ld),
                    "parallel": float(du * e_par[0] + dv * e_par[1]),
                    "perpendicular": float(du * e_perp[0] + dv * e_perp[1]),
                    "speed_used": spd,
                }
            )
    return {"samples": samples, "leads_min": [float(v) for v in leads]}


def fit_motion_error(
    samples_by_event: dict[str, list[dict[str, Any]]], leads_min: Sequence[float], *, source: str
) -> ens.MotionErrorStats:
    """Pooled standard deviation per lead (all events weighted alike: each event's samples are
    averaged first, so a long event does not dominate)."""
    par, perp = [], []
    for ld in leads_min:
        vp, vq = [], []
        for _, samples in samples_by_event.items():
            s = [x for x in samples if abs(x["lead_min"] - ld) < 1e-6]
            if len(s) < 2:
                continue
            vp.append(float(np.mean([x["parallel"] ** 2 for x in s])))
            vq.append(float(np.mean([x["perpendicular"] ** 2 for x in s])))
        par.append(math.sqrt(float(np.mean(vp))) if vp else float("nan"))
        perp.append(math.sqrt(float(np.mean(vq))) if vq else float("nan"))
    # hold the last finite value where no event reaches the lead
    par = _fill_tail(par)
    perp = _fill_tail(perp)
    n = sum(len(v) for v in samples_by_event.values())
    return ens.MotionErrorStats(
        leads_min=[float(v) for v in leads_min],
        parallel=par,
        perpendicular=perp,
        source=source,
        n_cases=n,
        extra={"events": sorted(samples_by_event)},
    )


def _fill_tail(values: list[float]) -> list[float]:
    out = list(values)
    last = next((v for v in out if math.isfinite(v)), 1.0)
    for i, v in enumerate(out):
        if math.isfinite(v):
            last = v
        else:
            out[i] = last
    return [round(float(v), 4) for v in out]


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def summarise(
    records: Sequence[vf.Record],
    *,
    reference: str = "eulerian",
    n_boot: int = 1000,
    seed: int = 0,
) -> dict[str, Any]:
    """Pooled scores per model and lead, and the paired bootstrap difference to the reference."""
    models = sorted({r.model for r in records})
    leads = sorted({r.lead_min for r in records})
    events = sorted({r.event for r in records})
    thr = sorted({t for r in records for t in r.cat})
    scales = sorted({s for r in records for (_, s) in r.fss})
    out: dict[str, Any] = {
        "models": models,
        "leads_min": leads,
        "events": events,
        "thresholds": thr,
        "fss_scales_cells": scales,
        "n_records": len(records),
        "by_model_lead": {},
        "skill_vs_reference": {},
    }
    for m in models:
        out["by_model_lead"][m] = {}
        for ld in leads:
            recs = [r for r in records if r.model == m and r.lead_min == ld]
            if recs:
                out["by_model_lead"][m][ld] = vf.pooled_scores(recs)
    for m in models:
        if m == reference:
            continue
        out["skill_vs_reference"][m] = {}
        for ld in leads:
            recs = [r for r in records if r.lead_min == ld and r.model in (m, reference)]
            if not recs:
                continue
            row: dict[str, Any] = {}
            for t in thr:
                row[f"csi_{t:g}"] = vf.skill_difference(
                    recs, m, reference, vf.stat_categorical("csi", t), n_boot=n_boot, seed=seed
                )
            mid = scales[len(scales) // 2] if scales else 1
            for t in thr:
                row[f"fss_{t:g}_{mid}"] = vf.skill_difference(
                    recs, m, reference, vf.stat_fss(t, mid), n_boot=n_boot, seed=seed
                )
            row["mae"] = vf.skill_difference(
                recs, m, reference, vf.stat_continuous("mae"), n_boot=n_boot, seed=seed
            )
            if any(r.prob for r in recs):
                for t in thr:
                    row[f"brier_{t:g}"] = vf.skill_difference(
                        recs, m, reference, vf.stat_prob("brier", t), n_boot=n_boot, seed=seed
                    )
            out["skill_vs_reference"][m][ld] = row
    return out

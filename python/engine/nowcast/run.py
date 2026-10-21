"""One call that makes a nowcast from a frame sequence, and the result it returns.

:func:`nowcast` estimates the motion (:mod:`engine.nowcast.motion`), extrapolates the latest
frame (:mod:`engine.nowcast.extrapolate`), builds the ensemble and probabilities
(:mod:`engine.nowcast.ensemble`), and returns a :class:`Nowcast` with everything needed to
judge, draw and save it: fields per lead time, the motion, the run record (every parameter and
default, the seed, the software versions) and the layer provenance of class ``forecast``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from engine import layer_provenance
from engine import provenance as prov
from engine.nowcast import ensemble as ens
from engine.nowcast import extrapolate as ex
from engine.nowcast import motion as mo
from engine.nowcast.frames import (
    FIELD_REFLECTIVITY,
    FrameSequence,
)

ProgressFn = Callable[[int, str], None]

DEFAULT_LEAD_MIN = 60.0
DEFAULT_STEP_MIN = 5.0
MAX_LEAD_MIN = 180.0
MAX_MEMBERS = 100
DEFAULT_DBZ_THRESHOLDS = (20.0, 35.0, 45.0)
DEFAULT_RATE_THRESHOLDS = (0.5, 2.0, 10.0)
#: dBZ given to an observed cell without echo before interpolation (below any threshold)
DRY_DBZ = -10.0

METHOD_TEXT = (
    "Advection nowcast: motion of the echo pattern from the last frames by dense pyramidal "
    "Lucas-Kanade optical flow (global cross-correlation fallback), backward semi-Lagrangian "
    "extrapolation of the latest frame with a frozen motion field (Lagrangian persistence: no "
    "growth, decay or initiation), ensemble from motion perturbations drawn from the verified "
    "motion error of past forecasts."
)
SKILL_TEXT_DEFAULT = (
    "Skill is case dependent and falls with lead time; see the verification page "
    "(docs: Nowcast) for the measured scores against persistence."
)
GUIDANCE_TEXT = "Experimental guidance, not an official forecast."


class NowcastError(Exception):
    """A nowcast could not be made from the given input."""


class NowcastCancelledError(NowcastError):
    """The run was cancelled."""


NowcastCancelled = NowcastCancelledError


@dataclass
class Nowcast:
    """A finished nowcast (fields are ``float32``; NaN = not observed / not forecastable)."""

    frames: FrameSequence  # the input sequence (its grid and metadata)
    origin_time: np.datetime64
    leads_min: np.ndarray  # (n_lead,)
    motion: mo.MotionField
    deterministic: np.ndarray  # (n_lead, ny, nx) in the input quantity (dBZ or mm/h)
    rain_rate: np.ndarray  # (n_lead, ny, nx) mm/h, deterministic member
    thresholds: tuple[float, ...]  # in the input quantity
    rate_thresholds: tuple[float, ...]  # mm/h
    probability: dict[float, np.ndarray]  # threshold (input quantity) -> (n_lead, ny, nx)
    rate_probability: dict[float, np.ndarray]  # mm/h threshold -> (n_lead, ny, nx)
    ensemble_mean_rate: np.ndarray | None  # (n_lead, ny, nx) mm/h
    spread_rate: np.ndarray | None  # (n_lead, ny, nx) mm/h, ensemble standard deviation
    members_rate: np.ndarray | None  # (n_member, n_lead, ny, nx) mm/h, member 0 unperturbed
    position_sigma_km: np.ndarray  # (n_lead,)
    motion_stats: ens.MotionErrorStats
    config: dict[str, Any]
    seed: int
    timing: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    skill_text: str = SKILL_TEXT_DEFAULT

    # -- facts -------------------------------------------------------------------------------
    @property
    def valid_times(self) -> np.ndarray:
        return self.origin_time + (self.leads_min * 60.0).astype("timedelta64[s]")

    @property
    def n_members(self) -> int:
        return 0 if self.members_rate is None else int(self.members_rate.shape[0])

    @property
    def field(self) -> str:
        return self.frames.field

    @property
    def units(self) -> str:
        return self.frames.units

    def lead_index(self, lead_min: float) -> int:
        return int(np.argmin(np.abs(self.leads_min - float(lead_min))))

    def stats(self) -> dict[str, Any]:
        """Per lead: covered fraction forecast, echo area (cells >= first threshold), max."""
        out = []
        thr = self.thresholds[0] if self.thresholds else None
        for k, lead in enumerate(self.leads_min):
            f = self.deterministic[k]
            fin = np.isfinite(f)
            out.append(
                {
                    "lead_min": float(lead),
                    "valid_time": prov.iso_utc(self.valid_times[k]),
                    "forecast_fraction": float(fin.mean()),
                    "echo_cells": int((f[fin] >= thr).sum()) if thr is not None else None,
                    "max": float(np.nanmax(f)) if fin.any() else None,
                    "position_sigma_km": float(self.position_sigma_km[k]),
                }
            )
        return {"leads": out, "n_members": self.n_members, "motion": self.motion.to_dict()}

    # -- provenance ------------------------------------------------------------------------
    def provenance(self) -> dict[str, Any]:
        """The ``forecast`` layer provenance (method, lead, skill statement, guidance label)."""
        lead_text = (
            f"{self.leads_min[0]:g} to {self.leads_min[-1]:g} min in steps of "
            f"{self.config.get('step_min'):g} min"
        )
        params = {
            "motion_method": self.motion.method,
            "n_frames": self.config.get("n_frames"),
            "lead_min": lead_text,
            "n_members": self.n_members,
            "seed": self.seed,
            "decay": self.config.get("decay", {}).get("model"),
            "zr_relation": self.config.get("zr_relation"),
            "motion_error_source": self.motion_stats.source,
        }
        return layer_provenance.forecast(
            source=self.frames.gridding.get("engine", "gridded frames")
            + (
                f": {', '.join(r['name'] for r in self.frames.radars[:6])}"
                if self.frames.radars
                else ""
            ),
            run_time=prov.utc_stamp(),
            origin_time=prov.iso_utc(self.origin_time),
            valid_end=prov.iso_utc(self.valid_times[-1]),
            lead_text=lead_text,
            method=METHOD_TEXT,
            skill=self.skill_text,
            params=params,
            inputs=self.frames.sources[-self.config.get("n_frames", 3) :],
            resolution=f"{self.frames.cell_m:g} m cells",
        ).to_dict()

    def record(self) -> dict[str, Any]:
        """The reproducible run record (JSON-able)."""
        return {
            "type": "FORECAST",
            "guidance": GUIDANCE_TEXT,
            "origin_time": prov.iso_utc(self.origin_time),
            "leads_min": [float(v) for v in self.leads_min],
            "valid_times": [prov.iso_utc(t) for t in self.valid_times],
            "field": self.field,
            "units": self.units,
            "method": METHOD_TEXT,
            "skill": self.skill_text,
            "motion": self.motion.to_dict(),
            "motion_error_statistics": self.motion_stats.to_dict(),
            "position_sigma_km": [round(float(v), 3) for v in self.position_sigma_km],
            "config": self.config,
            "seed": self.seed,
            "frames": self.frames.describe(),
            "stats": self.stats(),
            "timing": self.timing,
            "notes": self.notes,
            "provenance": self.provenance(),
            "created_with": prov.json_provenance(
                action="nowcast", params=self.config, source_files=self.frames.sources
            ),
        }

    # -- outputs ---------------------------------------------------------------------------
    def to_dataset(self) -> Any:
        from engine.nowcast import products

        return products.to_dataset(self)

    def save(self, out: Any) -> list[Path]:
        from engine.nowcast import products

        return products.save(self, out)

    def plot(self, **kwargs: Any) -> Any:
        from engine.nowcast import products

        return products.figure(self, **kwargs)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _leads(lead_min: float, step_min: float) -> np.ndarray:
    if not (step_min > 0) or not (lead_min > 0):
        raise NowcastError("lead and step must be positive minutes")
    if lead_min > MAX_LEAD_MIN:
        raise NowcastError(
            f"lead must be at most {MAX_LEAD_MIN:g} min (extrapolation has no skill beyond)"
        )
    if step_min > lead_min:
        raise NowcastError("step must not exceed the lead")
    n = round(lead_min / step_min)
    if abs(n * step_min - lead_min) > 1e-6:
        raise NowcastError("lead must be a multiple of step")
    return np.arange(1, n + 1, dtype=np.float64) * step_min


def nowcast(
    frames: FrameSequence,
    *,
    lead_min: float = DEFAULT_LEAD_MIN,
    step_min: float = DEFAULT_STEP_MIN,
    n_members: int = 0,
    motion_method: str = "lucas_kanade",
    n_frames: int = 3,
    origin_index: int | None = None,
    thresholds: Sequence[float] | None = None,
    rate_thresholds: Sequence[float] = DEFAULT_RATE_THRESHOLDS,
    zr_relation: str = "wsr88d",
    decay: Any = "persistence",
    motion_stats: ens.MotionErrorStats | str | Path | None = None,
    seed: int = 0,
    keep_members: bool = True,
    motion_options: dict[str, Any] | None = None,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Nowcast:
    """Make a nowcast from the frames up to ``origin_index`` (default: the last frame).

    ``n_members`` = 0 gives the deterministic nowcast only; otherwise member 0 is the
    unperturbed motion and the others perturb it (:mod:`engine.nowcast.ensemble`).
    ``thresholds`` are in the input quantity (dBZ or mm/h; defaults 20/35/45 dBZ or
    0.5/2/10 mm/h), ``rate_thresholds`` in mm/h for the rain-rate probabilities.
    """
    t_start = time.perf_counter()
    report = progress or (lambda _p, _m: None)

    def check() -> None:
        if cancel and cancel():
            raise NowcastCancelled("cancelled")

    if frames.n < 1:
        raise NowcastError("no frames")
    if n_members < 0 or n_members > MAX_MEMBERS:
        raise NowcastError(f"n_members must be between 0 and {MAX_MEMBERS}")
    end = frames.n if origin_index is None else int(origin_index) + 1
    if not 1 <= end <= frames.n:
        raise NowcastError("origin_index is out of range")
    leads = _leads(float(lead_min), float(step_min))
    stats = (
        motion_stats
        if isinstance(motion_stats, ens.MotionErrorStats)
        else ens.MotionErrorStats.load(motion_stats)
    )
    dec = ex.decay_model(decay)
    is_refl = frames.field == FIELD_REFLECTIVITY
    thr = tuple(
        float(t)
        for t in (thresholds or (DEFAULT_DBZ_THRESHOLDS if is_refl else DEFAULT_RATE_THRESHOLDS))
    )
    rthr = tuple(float(t) for t in rate_thresholds)
    notes: list[str] = []

    # 1. motion ----------------------------------------------------------------------------
    report(5, "Estimating the motion")
    first = max(0, end - max(2, n_frames))
    sub = frames.subset(range(first, end))
    t_m0 = time.perf_counter()
    motion = mo.estimate_motion(
        sub.data,
        sub.times,
        frames.cell_m,
        method=motion_method,
        n_frames=n_frames,
        floor=mo.DEFAULT_FLOOR_DBZ if is_refl else 0.1,
        **(motion_options or {}),
    )
    t_motion = time.perf_counter() - t_m0
    notes += motion.notes
    if sub.n < 2 and motion_method != "none":
        notes.append("a single frame: zero motion (Eulerian persistence)")
    check()

    # 2. extrapolation ---------------------------------------------------------------------
    report(25, "Extrapolating the latest frame")
    latest = frames.data[end - 1]
    origin_time = frames.times[end - 1]
    covered = frames.coverage
    floor = DRY_DBZ if is_refl else 0.0
    leads_s = leads * 60.0
    t_e0 = time.perf_counter()

    def run_member(du: np.ndarray, dv: np.ndarray) -> np.ndarray:
        """Extrapolate with the motion perturbed per lead by (du, dv) m/s (constant in space)."""
        if np.all(du == 0) and np.all(dv == 0):
            return ex.extrapolate(
                latest, motion.u, motion.v, frames.cell_m, leads_s, floor=floor, observed=covered
            )
        # a perturbation that changes with lead is a different frozen field per lead: run the
        # steps one at a time, carrying the displacement
        out = np.full((leads.size, *latest.shape), np.nan, dtype=np.float32)
        steps = np.diff(np.concatenate([[0.0], leads_s]))
        u_c = (motion.u.astype(np.float64)) / frames.cell_m
        v_c = (motion.v.astype(np.float64)) / frames.cell_m
        ny, nx = latest.shape
        yy, xx = np.mgrid[0:ny, 0:nx].astype(np.float64)
        f = np.asarray(latest, dtype=np.float64)
        base = np.where(np.isfinite(f) & covered, f, floor)
        obs = covered.astype(np.float64)
        dx = np.zeros((ny, nx))
        dy = np.zeros((ny, nx))
        for k, dt in enumerate(steps):
            uk = u_c + du[k] / frames.cell_m
            vk = v_c + dv[k] / frames.cell_m
            # one more step along the trajectory, from the current departure point
            sx = ex._sample(uk, yy - dy, xx - dx, 0.0) * dt
            sy = ex._sample(vk, yy - dy, xx - dx, 0.0) * dt
            for _ in range(2):
                sx = ex._sample(uk, yy - dy - 0.5 * sy, xx - dx - 0.5 * sx, 0.0) * dt
                sy = ex._sample(vk, yy - dy - 0.5 * sy, xx - dx - 0.5 * sx, 0.0) * dt
            dx = dx + sx
            dy = dy + sy
            ys, xs = yy - dy, xx - dx
            vals = ex._sample(base, ys, xs, floor)
            known = ex._sample(obs, ys, xs, 0.0)
            inside = (ys >= 0) & (ys <= ny - 1) & (xs >= 0) & (xs <= nx - 1)
            out[k] = np.where(inside & (known >= 0.5), vals, np.nan)
        return out

    zero = np.zeros(leads.size)
    det = run_member(zero, zero)
    t_extrap = time.perf_counter() - t_e0
    check()

    def to_rate(arr: np.ndarray) -> np.ndarray:
        r = (
            ex.dbz_to_rate(arr, zr_relation)
            if is_refl
            else np.where(np.isfinite(arr), np.maximum(arr, 0.0), np.nan).astype(np.float32)
        )
        for k, ls in enumerate(leads_s):
            r[k] = dec.apply(r[k], float(ls))
        return r

    det_rate = to_rate(det)
    if is_refl and not isinstance(dec, ex.Persistence):
        # a decay acts on the rain rate: the deterministic dBZ field follows it
        det = ex.rate_to_dbz(det_rate, zr_relation)
        det = np.where(np.isfinite(det_rate) & ~np.isfinite(det), floor, det).astype(np.float32)

    # 3. ensemble --------------------------------------------------------------------------
    members_rate = None
    ens_mean = None
    spread = None
    prob: dict[float, np.ndarray] = {}
    rate_prob: dict[float, np.ndarray] = {}
    pos_sigma = np.array([ens.position_sigma_m(stats, float(ld)) / 1000.0 for ld in leads])
    t_s0 = time.perf_counter()
    if n_members > 0:
        report(40, f"Running {n_members} perturbed members")
        mean_uv = motion.mean_uv(np.isfinite(latest) & covered)
        pert = ens.perturbations(n_members, mean_uv, stats, leads, seed=seed)
        mem = np.empty((n_members, leads.size, *latest.shape), dtype=np.float32)
        mem[0] = det
        for m in range(1, n_members):
            check()
            mem[m] = run_member(pert[m, :, 0], pert[m, :, 1])
            if m % 5 == 0:
                report(40 + int(40 * m / n_members), f"Member {m + 1}/{n_members}")
        members_rate = np.stack([to_rate(mem[m]) for m in range(n_members)])
        members_rate[0] = det_rate
        known = np.isfinite(members_rate).any(axis=0)
        filled = np.where(np.isfinite(members_rate), members_rate, 0.0)
        count = np.maximum(np.isfinite(members_rate).sum(axis=0), 1)
        ens_mean = np.where(known, filled.sum(axis=0) / count, np.nan).astype(np.float32)
        var = (
            np.where(np.isfinite(members_rate), (members_rate - ens_mean[None]) ** 2, 0.0).sum(
                axis=0
            )
            / count
        )
        spread = np.where(known, np.sqrt(var), np.nan).astype(np.float32)
        for t in thr:
            prob[t] = np.stack(
                [
                    ens.exceedance_probability(
                        mem[:, k], t, smooth_cells=pos_sigma[k] * 1000.0 / frames.cell_m
                    )
                    for k in range(leads.size)
                ]
            )
        for t in rthr:
            rate_prob[t] = np.stack(
                [
                    ens.exceedance_probability(
                        members_rate[:, k], t, smooth_cells=pos_sigma[k] * 1000.0 / frames.cell_m
                    )
                    for k in range(leads.size)
                ]
            )
        if not keep_members:
            members_rate = None
    else:
        # deterministic only: the neighbourhood probability of the single member, so that the
        # probability fields exist (and say in the record that they come from one member)
        for t in thr:
            prob[t] = np.stack(
                [
                    ens.exceedance_probability(
                        det[k][None], t, smooth_cells=pos_sigma[k] * 1000.0 / frames.cell_m
                    )
                    for k in range(leads.size)
                ]
            )
        for t in rthr:
            rate_prob[t] = np.stack(
                [
                    ens.exceedance_probability(
                        det_rate[k][None], t, smooth_cells=pos_sigma[k] * 1000.0 / frames.cell_m
                    )
                    for k in range(leads.size)
                ]
            )
        notes.append(
            "no ensemble: the probabilities are the neighbourhood probability of the single "
            "deterministic member (position uncertainty only)"
        )
    t_ens = time.perf_counter() - t_s0

    config = {
        "lead_min": float(lead_min),
        "step_min": float(step_min),
        "n_members": int(n_members),
        "motion_method": motion_method,
        "n_frames": int(n_frames),
        "motion_options": dict(motion_options or {}),
        "motion_parameters": motion.parameters,
        "thresholds": list(thr),
        "rate_thresholds": list(rthr),
        "zr_relation": zr_relation if is_refl else None,
        "zr_text": ex.zr_coefficients(zr_relation)[2] if is_refl else None,
        "decay": dec.describe(),
        "dry_fill": floor,
        "interpolation": "bilinear, backward semi-Lagrangian, 3 fixed-point iterations",
        "probability_neighbourhood": (
            "Gaussian, sigma = lead x verified motion error (position sigma)"
        ),
        "origin_index": end - 1,
    }
    timing = {
        "motion_s": round(t_motion, 3),
        "extrapolation_s": round(t_extrap, 3),
        "ensemble_s": round(t_ens, 3),
        "total_s": round(time.perf_counter() - t_start, 3),
    }
    report(96, "Nowcast ready")
    return Nowcast(
        frames=frames,
        origin_time=origin_time,
        leads_min=leads,
        motion=motion,
        deterministic=det,
        rain_rate=det_rate,
        thresholds=thr,
        rate_thresholds=rthr,
        probability=prob,
        rate_probability=rate_prob,
        ensemble_mean_rate=ens_mean,
        spread_rate=spread,
        members_rate=members_rate,
        position_sigma_km=pos_sigma,
        motion_stats=stats,
        config=config,
        seed=int(seed),
        timing=timing,
        notes=sorted(set(notes)),
    )


def eulerian_persistence(
    frames: FrameSequence, leads_min: np.ndarray, origin_index: int | None = None
) -> np.ndarray:
    """The baseline: the latest frame held at every lead (``(n_lead, ny, nx)``)."""
    end = frames.n if origin_index is None else int(origin_index) + 1
    latest = frames.data[end - 1]
    out = np.repeat(latest[None], len(leads_min), axis=0).astype(np.float32)
    if frames.covered is not None:
        out[:, ~frames.covered] = np.nan
    return out


def is_finite_number(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False

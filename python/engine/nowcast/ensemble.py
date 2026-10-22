"""Uncertainty of the extrapolation as an ensemble of perturbed motions.

The dominant error of a short-range extrapolation is where the echo ends up, i.e. the error of
the motion (Bowler et al. 2006 built the STEPS velocity perturbations on this). The ensemble
here perturbs the motion field of every member by one vector, drawn in the frame of the mean
motion (a component along it, "parallel", and one across it, "perpendicular"), with a standard
deviation that is the **verified motion error** of this very method on past cases: the
difference between the motion used at the forecast time and the motion the echo really had over
the following lead time, measured by :mod:`engine.nowcast.verify` on tuning events that are not
the verification events (``docs/NOWCAST.md``). One draw per member is scaled with the lead-time
dependent standard deviation, so a member is a consistent trajectory (the same choice as the
STEPS/pysteps velocity perturbator, Bowler et al. 2006; Pulkkinen et al. 2019).

From the members: the ensemble mean rain rate (a mean of rates, never of dBZ), the probability
of exceeding thresholds (the member fraction, then a Gaussian neighbourhood whose width is the
position uncertainty ``lead x sigma_motion`` implied by the same statistics: a neighbourhood
probability, Theis et al. 2005), the ensemble spread, and the position-uncertainty radius per
lead.

What the ensemble does **not** contain: growth and decay, initiation, local (cell-by-cell)
motion errors, radar errors. The verification reports the spread/skill ratio so that this
under-dispersion is measured, not hidden.

References
----------
* Bowler, N. E., C. E. Pierce, and A. W. Seed, 2006: STEPS: A probabilistic precipitation
  forecasting scheme which merges an extrapolation nowcast with downscaled NWP. *Q. J. R.
  Meteorol. Soc.*, **132**, 2127-2155.
* Pulkkinen, S., et al., 2019: Pysteps: an open-source Python library for probabilistic
  precipitation nowcasting (v1.0). *Geosci. Model Dev.*, **12**, 4185-4219.
* Theis, S. E., A. Hense, and U. Damrath, 2005: Probabilistic precipitation forecasts from a
  deterministic model: a pragmatic approach. *Meteorol. Appl.*, **12**, 257-268.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from engine._lazy import LazyModule

ndimage = LazyModule("scipy.ndimage")

_HERE = Path(__file__).resolve().parent
DEFAULT_STATS_FILE = _HERE / "motion_error_default.json"


@dataclass
class MotionErrorStats:
    """Standard deviation (m/s) of the motion error as a function of lead time (minutes).

    ``parallel`` / ``perpendicular`` are the components along and across the forecast motion.
    Values between the tabulated leads are interpolated linearly; beyond the last lead the last
    value is held (said in the record). ``source`` says where the numbers come from.
    """

    leads_min: list[float]
    parallel: list[float]
    perpendicular: list[float]
    source: str = ""
    n_cases: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def sigma(self, lead_min: float) -> tuple[float, float]:
        """The two standard deviations at ``lead_min``, such that the position uncertainty
        ``lead x sigma`` never shrinks with lead: where the table falls faster than 1/lead (the
        error measured over a short lead is one noisy frame-pair estimate, over a long lead the
        mean of many), the position uncertainty reached earlier is carried forward."""
        if not self.leads_min:
            return 0.0, 0.0
        leads = np.asarray(self.leads_min, dtype=np.float64)
        out = []
        for comp in (self.parallel, self.perpendicular):
            pos = np.maximum.accumulate(leads * np.asarray(comp, dtype=np.float64))
            if lead_min <= 0:
                out.append(float(comp[0]))
            elif lead_min >= leads[-1]:
                out.append(float(pos[-1] / leads[-1]))  # the last value is held
            else:
                p = float(
                    np.interp(
                        lead_min, np.concatenate(([0.0], leads)), np.concatenate(([0.0], pos))
                    )
                )
                out.append(p / lead_min)
        return out[0], out[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "leads_min": list(self.leads_min),
            "parallel_ms": list(self.parallel),
            "perpendicular_ms": list(self.perpendicular),
            "source": self.source,
            "n_cases": self.n_cases,
            **self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MotionErrorStats:
        return cls(
            leads_min=[float(x) for x in d["leads_min"]],
            parallel=[float(x) for x in d["parallel_ms"]],
            perpendicular=[float(x) for x in d["perpendicular_ms"]],
            source=str(d.get("source", "")),
            n_cases=int(d.get("n_cases", 0)),
            extra={
                k: v
                for k, v in d.items()
                if k not in ("leads_min", "parallel_ms", "perpendicular_ms", "source", "n_cases")
            },
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> MotionErrorStats:
        p = Path(path) if path else DEFAULT_STATS_FILE
        if not p.exists():
            # No verified statistics shipped: say so and use a conservative, stated placeholder.
            return cls(
                leads_min=[5.0, 60.0],
                parallel=[2.0, 2.0],
                perpendicular=[2.0, 2.0],
                source="placeholder (no verified statistics file found): 2 m/s at every lead",
            )
        with open(p, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def perturbations(
    n_members: int,
    mean_uv: tuple[float, float],
    stats: MotionErrorStats,
    leads_min: Sequence[float],
    *,
    seed: int = 0,
) -> np.ndarray:
    """``(n_members, n_lead, 2)`` perturbation vectors (du, dv) in m/s; member 0 is unperturbed.

    One standard-normal pair per member is drawn once and scaled with ``stats.sigma(lead)``, in
    the parallel / perpendicular frame of ``mean_uv`` (east if the mean motion is zero).
    """
    rng = np.random.default_rng(seed)
    u0, v0 = mean_uv
    spd = math.hypot(u0, v0)
    e_par = np.array([u0, v0]) / spd if spd > 1e-6 else np.array([1.0, 0.0])
    e_perp = np.array([-e_par[1], e_par[0]])
    z = rng.standard_normal((n_members, 2))
    z[0] = 0.0
    out = np.zeros((n_members, len(leads_min), 2), dtype=np.float64)
    for k, lead in enumerate(leads_min):
        sp, sq = stats.sigma(float(lead))
        out[:, k, :] = (z[:, 0:1] * sp) * e_par[None, :] + (z[:, 1:2] * sq) * e_perp[None, :]
    return out


def exceedance_probability(
    members: np.ndarray, threshold: float, *, smooth_cells: float = 0.0
) -> np.ndarray:
    """Fraction of members ``>= threshold`` (NaN where any member is unobserved), then a
    Gaussian neighbourhood of ``smooth_cells`` standard deviation (0 = none)."""
    known = np.isfinite(members).all(axis=0)
    hit = np.where(np.isfinite(members), members >= threshold, False)
    p = hit.mean(axis=0).astype(np.float64)
    if smooth_cells > 0.25:
        w = known.astype(np.float64)
        num = ndimage.gaussian_filter(np.where(known, p, 0.0), smooth_cells, mode="constant")
        den = ndimage.gaussian_filter(w, smooth_cells, mode="constant")
        with np.errstate(invalid="ignore", divide="ignore"):
            p = np.where(den > 1e-6, num / den, p)
    return np.where(known, np.clip(p, 0.0, 1.0), np.nan).astype(np.float32)


def position_sigma_m(stats: MotionErrorStats, lead_min: float) -> float:
    """Position uncertainty (1 sigma, m) implied by the motion error at ``lead_min``: the
    root-mean-square of the two components times the lead time."""
    sp, sq = stats.sigma(lead_min)
    return math.sqrt(0.5 * (sp * sp + sq * sq)) * lead_min * 60.0

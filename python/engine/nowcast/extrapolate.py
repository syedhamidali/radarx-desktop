"""Semi-Lagrangian extrapolation of the latest frame along a frozen motion field.

The value forecast at cell ``x`` and lead ``t`` is the value of the latest frame at the point
the air came from: ``F(x, t) = R0(x - D(x, t))`` (backward semi-Lagrangian scheme, Germann and
Zawadzki 2002). The displacement ``D`` follows the motion field step by step with the mid-point
fixed-point iteration of Staniforth and Cote (1991): for each step of ``dt`` seconds
``D <- D + dt * V(x - D - dt V(...)/2)``, ``n_iter`` times. Values are read from the **original**
frame once per lead time with bilinear interpolation (no compounding of interpolation errors),
the way pysteps does it (Pulkkinen et al. 2019).

What this does, and does not, do:

* the motion field is **frozen** (the same at every lead): Lagrangian persistence;
* the frame is moved, **not changed**: no growth, no decay, no initiation. A growth/decay model
  can be plugged in (:class:`Decay`) and the default is ``"persistence"`` (none);
* a cell whose departure point lies outside the observed domain (or outside the radar coverage)
  is **not observed**: it stays NaN instead of being filled with a guess.

Reflectivity is interpolated in dBZ (a logarithmic quantity; interpolating between two
cells in dBZ keeps the shape of a gradient as the radar shows it) and converted to a rain rate
afterwards with a documented Z-R relation of the engine (:mod:`engine.hydro.qpe`).

References
----------
* Germann, U., and I. Zawadzki, 2002: Scale-dependence of the predictability of precipitation
  from continental radar images. Part I: Description of the methodology. *Mon. Wea. Rev.*, **130**,
  2859-2873.
* Staniforth, A., and J. Cote, 1991: Semi-Lagrangian integration schemes for atmospheric models:
  A review. *Mon. Wea. Rev.*, **119**, 2206-2223.
* Pulkkinen, S., et al., 2019: Pysteps: an open-source Python library for probabilistic
  precipitation nowcasting (v1.0). *Geosci. Model Dev.*, **12**, 4185-4219.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from engine._lazy import LazyModule

ndimage = LazyModule("scipy.ndimage")

#: a reflectivity below this is "no rain" (0 mm/h) in the conversions below
MIN_RAIN_DBZ = 5.0


# ---------------------------------------------------------------------------
# Z-R conversions (the engine's documented relations)
# ---------------------------------------------------------------------------


def zr_coefficients(relation: str = "wsr88d") -> tuple[float, float, str]:
    """``(A, beta, text)`` of ``Z = A R^beta`` for a named relation of the rainfall engine."""
    from engine.hydro.qpe import zr_relation

    r = zr_relation(relation)
    return float(r.A), float(r.beta), r.text()


def dbz_to_rate(dbz: np.ndarray, relation: str = "wsr88d") -> np.ndarray:
    """Rain rate (mm/h) from reflectivity (dBZ); below :data:`MIN_RAIN_DBZ` it is 0; NaN stays."""
    a, b, _ = zr_coefficients(relation)
    z = np.asarray(dbz, dtype=np.float64)
    with np.errstate(invalid="ignore", over="ignore"):
        rate = np.power(np.power(10.0, z / 10.0) / a, 1.0 / b)
    rate = np.where(z < MIN_RAIN_DBZ, 0.0, rate)
    return np.where(np.isfinite(z), rate, np.nan).astype(np.float32)


def rate_to_dbz(rate: np.ndarray, relation: str = "wsr88d") -> np.ndarray:
    """Reflectivity (dBZ) of a rain rate (mm/h); 0 mm/h gives NaN (no echo)."""
    a, b, _ = zr_coefficients(relation)
    r = np.asarray(rate, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = 10.0 * np.log10(a * np.power(np.maximum(r, 0.0), b))
    return np.where(np.isfinite(r) & (r > 0), z, np.nan).astype(np.float32)


# ---------------------------------------------------------------------------
# Growth / decay interface
# ---------------------------------------------------------------------------


class Decay(Protocol):
    """How the extrapolated intensity changes with lead time (a clean, named interface).

    ``apply(values, lead_s)`` returns the modified values (same shape, NaN kept). Models must be
    deterministic and describe themselves in ``describe()`` (shown in the provenance record).
    """

    def apply(self, values: np.ndarray, lead_s: float) -> np.ndarray: ...

    def describe(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Persistence:
    """Lagrangian persistence: the intensity is kept as it is (the documented default)."""

    def apply(self, values: np.ndarray, lead_s: float) -> np.ndarray:
        return values

    def describe(self) -> dict[str, Any]:
        return {"model": "persistence", "text": "intensity kept (Lagrangian persistence)"}


@dataclass(frozen=True)
class ExponentialDecay:
    """Rain rate multiplied by ``exp(-lead / tau)``: a crude, stated decay with time scale
    ``tau_s``; applied to rain rate (mm/h), never to dBZ. Off unless asked for."""

    tau_s: float

    def apply(self, values: np.ndarray, lead_s: float) -> np.ndarray:
        if self.tau_s <= 0:
            return values
        return (values * float(np.exp(-lead_s / self.tau_s))).astype(values.dtype)

    def describe(self) -> dict[str, Any]:
        return {
            "model": "exponential_decay",
            "tau_s": self.tau_s,
            "text": f"rain rate x exp(-lead/{self.tau_s:g} s)",
        }


def decay_model(spec: Any) -> Decay:
    """``"persistence"`` (default), ``"exponential:<tau_s>"`` or a :class:`Decay` object."""
    if spec is None or spec == "persistence":
        return Persistence()
    if isinstance(spec, str) and spec.startswith("exponential"):
        _, _, tau = spec.partition(":")
        try:
            return ExponentialDecay(float(tau or 3600.0))
        except ValueError as exc:
            raise ValueError(f"decay {spec!r}: give exponential:<seconds>") from exc
    if hasattr(spec, "apply") and hasattr(spec, "describe"):
        return spec  # type: ignore[return-value]
    raise ValueError(f"unknown growth/decay model {spec!r}")


# ---------------------------------------------------------------------------
# Semi-Lagrangian displacement and sampling
# ---------------------------------------------------------------------------


def _sample(field2d: np.ndarray, yy: np.ndarray, xx: np.ndarray, cval: float) -> np.ndarray:
    return ndimage.map_coordinates(
        np.asarray(field2d, dtype=np.float64),
        np.array([yy, xx]),
        order=1,
        mode="constant",
        cval=cval,
    )


def displacements(
    u_cells_s: np.ndarray,
    v_cells_s: np.ndarray,
    lead_steps_s: Sequence[float],
    *,
    n_iter: int = 3,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Backward displacement ``(dx, dy)`` in cells for every lead, following the motion field.

    ``u_cells_s`` / ``v_cells_s`` are the motion in **cells per second** (x east, y north);
    ``lead_steps_s`` the successive step lengths in seconds (the displacement of lead ``k`` is
    the sum of the first ``k + 1`` steps along the trajectory).
    """
    ny, nx = u_cells_s.shape
    yy, xx = np.mgrid[0:ny, 0:nx].astype(np.float64)
    dx = np.zeros((ny, nx))
    dy = np.zeros((ny, nx))
    out = []
    for dt in lead_steps_s:
        # mid-point fixed-point iteration (Staniforth and Cote 1991)
        sx = _sample(u_cells_s, yy - dy, xx - dx, 0.0) * dt
        sy = _sample(v_cells_s, yy - dy, xx - dx, 0.0) * dt
        for _ in range(max(0, n_iter - 1)):
            sx = _sample(u_cells_s, yy - dy - 0.5 * sy, xx - dx - 0.5 * sx, 0.0) * dt
            sy = _sample(v_cells_s, yy - dy - 0.5 * sy, xx - dx - 0.5 * sx, 0.0) * dt
        dx = dx + sx
        dy = dy + sy
        out.append((dx.copy(), dy.copy()))
    return out


def extrapolate(
    frame: np.ndarray,
    u_ms: np.ndarray,
    v_ms: np.ndarray,
    cell_m: float,
    leads_s: Sequence[float],
    *,
    floor: float | None = None,
    observed: np.ndarray | None = None,
    n_iter: int = 3,
    min_observed: float = 0.5,
) -> np.ndarray:
    """Extrapolate ``frame`` (2-D, NaN = no echo or not observed) to each lead (seconds).

    ``floor`` is the value an observed-but-dry cell takes before interpolation (for dBZ a low
    number such as -10; for a rain rate 0); cells where ``observed`` is False (default: where the
    frame is NaN) are treated as unknown: a forecast cell whose departure point has less than
    ``min_observed`` of its bilinear weight on observed cells is NaN. Returns ``(n_lead, ny, nx)``
    float32.
    """
    f = np.asarray(frame, dtype=np.float64)
    ny, nx = f.shape
    obs = np.isfinite(f) if observed is None else np.asarray(observed, dtype=bool)
    fill = 0.0 if floor is None else float(floor)
    base = np.where(np.isfinite(f), f, fill)
    base = np.where(obs, base, fill)
    leads = np.asarray(leads_s, dtype=np.float64)
    if leads.ndim != 1 or leads.size == 0:
        raise ValueError("leads_s must be a non-empty 1-D sequence of seconds")
    if np.any(np.diff(leads) <= 0) or leads[0] <= 0:
        raise ValueError("leads_s must be positive and increasing")
    steps = np.diff(np.concatenate([[0.0], leads]))
    u_c = np.asarray(u_ms, dtype=np.float64) / float(cell_m)
    v_c = np.asarray(v_ms, dtype=np.float64) / float(cell_m)
    yy, xx = np.mgrid[0:ny, 0:nx].astype(np.float64)
    out = np.full((leads.size, ny, nx), np.nan, dtype=np.float32)
    for k, (dx, dy) in enumerate(displacements(u_c, v_c, steps, n_iter=n_iter)):
        ys, xs = yy - dy, xx - dx
        vals = _sample(base, ys, xs, fill)
        known = _sample(obs.astype(np.float64), ys, xs, 0.0)
        inside = (ys >= 0) & (ys <= ny - 1) & (xs >= 0) & (xs <= nx - 1)
        ok = inside & (known >= min_observed)
        out[k] = np.where(ok, vals, np.nan)
    return out

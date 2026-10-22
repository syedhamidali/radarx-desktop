"""Ensembles of rainfall-estimate errors: spread, quantiles and exceedance probabilities.

What this is
------------
The radar accumulation ``A(x)`` (mm) is one estimate. The ensemble is a set of ``N`` equally likely
*possible truths* given that estimate and an **assumed error model**, so that a user can ask "how
likely is
it that at least 50 mm fell here?" and not only "what is the best estimate here?". The probabilities
are
conditional on the error model: they are not calibrated unless a reliability check against
independent
gauges says so (:func:`reliability`), and the model contains only the errors it lists.

Error model
-----------
Multiplicative, lognormal, spatially correlated, in the log domain (the approach of the radar
ensemble
generator REAL of Germann et al. 2009, which models the radar error as the log ratio of gauge and
radar and
its spatial structure with an exponential semivariogram; the construction here is our own and
simpler)::

    A_m(x) = A(x) * exp( s w(x) z_m(x) - s^2 w(x)^2 / 2 )        member m

* ``s`` is the standard deviation of ``ln(truth / estimate)``; the ``- s^2 w^2 / 2`` makes the
  factor have
  expectation 1, so the **ensemble mean equals the estimate** (rain is a linear quantity, depths in
  mm are
  averaged as they are; a decibel quantity would be averaged in linear units, see
  ``docs/SCIENCE.md``),
* ``z_m`` is a Gaussian field with unit variance made of three independent parts: one number for the
  whole domain (a calibration or Z-R bias is common to every cell), a field with an exponential
  correlation
  ``exp(-h / L)`` (the error of one storm or one beam geometry is similar nearby), and cell-to-cell
  noise
  (nugget). ``shares`` are their shares of the variance,
* ``w(x)`` scales the error cell by cell with the engine's own uncertainty field (the relative 1
  sigma of the
  accumulation: larger at long range, in hail, behind attenuation) and is 1 where there is none.

Where the numbers come from (each one is reported, with where it came from)
---------------------------------------------------------------------------
* **With gauges**: ``s`` is the root mean square of ``ln(gauge / radar)`` over the gauges with at
  least
  ``min_amount`` mm on both (so a bias between gauges and radar is part of the error), the shares
  and the
  correlation length from an exponential variogram of the deviations when at least 8 gauges are
  available.
* **Without gauges**: ``s`` is taken from the engine's own uncertainty model (median relative 1
  sigma of the
  accumulation, converted to a log standard deviation); the shares ``(0.3, 0.5, 0.2)`` and the
  correlation
  length ``25 km`` are **assumed defaults**, not measurements and not literature values. They are
  marked as
  such in the output; change them (``--error-corr-km``) and see how the probabilities move.

What it does not contain
------------------------
Anything the estimate's own uncertainty field does not contain either (vertical profile of
reflectivity,
bright band, overshoot, advection and sampling errors in time), gauge errors, errors that are not
multiplicative, and rain where the radar saw none: a cell with 0 mm stays 0 mm in every member.

Reproducible: member ``k`` is drawn from ``SeedSequence(seed).spawn(N)[k]``, so the same seed and
options give
the same members on any machine, and adding members does not change the first ones.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from engine import provenance as prov
from engine.hydro.qpe import QpeError


def _radarx(name: str) -> Any:
    """A function of ``radarx.retrieve`` when the installed radarx has it, else None.

    The algorithms live in radarx (``qpe_error_ensemble``, ``qpe_error_model``,
    ``exceedance_probability``, ``reliability``, ``brier_score``); this module wraps them with the
    app's own parts (what comes from gauges and what is assumed, the labels, the run record). The
    built-in copies are a **transitional fallback** for radarx versions that do not have the
    functions yet; they are tested to agree and go when the minimum radarx version has them.
    """
    try:
        import radarx.retrieve as rt
    except ImportError:
        return None
    return getattr(rt, name, None)


def uses_radarx() -> dict[str, bool]:
    """Which radarx functions are in use (the others run the built-in fallback)."""
    names = (
        "qpe_error_ensemble",
        "qpe_error_model",
        "exceedance_probability",
        "reliability",
        "brier_score",
    )
    return {n: _radarx(n) is not None for n in names}


#: assumed (not measured, not from the literature) when there are no gauges
DEFAULT_CORR_LENGTH_KM = 25.0
DEFAULT_SHARES = (0.3, 0.5, 0.2)  # common to the domain, spatially correlated, cell-to-cell
DEFAULT_THRESHOLDS = (5.0, 10.0, 25.0, 50.0, 100.0)  # mm
DEFAULT_QUANTILES = (0.05, 0.5, 0.95)
#: members are kept in memory (float32); refuse more than this many bytes
MAX_MEMBER_BYTES = 400 * 1024 * 1024
MIN_GAUGES = 5
MIN_GAUGES_VARIOGRAM = 8
MIN_GAUGES_RELIABILITY = 30

DISCLAIMER = (
    "An ensemble of possible truths under an ASSUMED error model, not a forecast and not an "
    "observation. Probabilities are conditional on that model and are not calibrated unless the "
    "reliability check against gauges says so."
)


class EnsembleError(QpeError):
    """A wrong ensemble option or a case the ensemble cannot be built for."""


# ---------------------------------------------------------------------------
# Small conversions
# ---------------------------------------------------------------------------


def sigma_ln_from_cv(cv: float) -> float:
    """Standard deviation of ln(X) of a lognormal variable with coefficient of variation ``cv``."""
    return math.sqrt(math.log1p(float(cv) ** 2))


def sigma_ln_from_db(sigma_db: float, b: float = 1.0 / 1.4) -> float:
    """sigma of ln(R) from a reflectivity error of ``sigma_db`` dB for R = a Z^b (default b of the
    WSR-88D relation): ``b ln(10) / 10 * sigma_db``."""
    return float(b) * math.log(10.0) / 10.0 * float(sigma_db)


# ---------------------------------------------------------------------------
# The error model
# ---------------------------------------------------------------------------


@dataclass
class ErrorModel:
    """Parameters of the error model and where each one came from."""

    sigma_ln: float
    shares: tuple[float, float, float]
    corr_length_km: float
    scale_by_uncertainty: bool = True
    sources: dict[str, str] = field(default_factory=dict)
    gauge_stats: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not (math.isfinite(self.sigma_ln) and 0.0 < self.sigma_ln <= 3.0):
            raise EnsembleError(
                f"the error standard deviation of ln(truth/estimate) must be in (0, 3], not "
                f"{self.sigma_ln!r} (0.3 is about 30 %)"
            )
        sh = np.asarray(self.shares, dtype=float)
        if (
            sh.shape != (3,)
            or np.any(sh < 0)
            or not math.isclose(float(sh.sum()), 1.0, abs_tol=1e-6)
        ):
            raise EnsembleError(
                "the variance shares (common, spatial, cell to cell) must be >= 0 and add up to 1"
            )
        if not (math.isfinite(self.corr_length_km) and self.corr_length_km > 0):
            raise EnsembleError("the error correlation length must be positive (km)")

    def to_dict(self) -> dict[str, Any]:
        return {
            "form": "multiplicative lognormal, mean-preserving, spatially correlated",
            "sigma_ln": self.sigma_ln,
            "shares": {
                "common": self.shares[0],
                "spatial": self.shares[1],
                "cell_to_cell": self.shares[2],
            },
            "corr_length_km": self.corr_length_km,
            "scale_by_uncertainty": self.scale_by_uncertainty,
            "sources": dict(self.sources),
            "gauge_stats": self.gauge_stats,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ErrorModel:
        sh = d["shares"]
        return cls(
            sigma_ln=float(d["sigma_ln"]),
            shares=(float(sh["common"]), float(sh["spatial"]), float(sh["cell_to_cell"])),
            corr_length_km=float(d["corr_length_km"]),
            scale_by_uncertainty=bool(d.get("scale_by_uncertainty", True)),
            sources=dict(d.get("sources", {})),
            gauge_stats=d.get("gauge_stats"),
            notes=list(d.get("notes", [])),
        )


def relative_sigma_ln(result: Any, field_name: str = "accumulation") -> np.ndarray:
    """Cell-by-cell ``sigma(ln A)`` from the engine's uncertainty field (NaN where not defined)."""
    acc = np.asarray(getattr(result, field_name), dtype=np.float64)
    sig = np.asarray(result.accumulation_sigma, dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        cv = np.where(acc >= 0.5, sig / acc, np.nan)
    return np.sqrt(np.log1p(cv**2))


def _weights(result: Any, field_name: str, scale: bool) -> tuple[np.ndarray | float, float | None]:
    """``(w, median)``: the cell scaling of the error (1 where unknown) and the median sigma."""
    if not scale:
        return 1.0, None
    rel = relative_sigma_ln(result, field_name)
    ok = np.isfinite(rel) & (rel > 0)
    if not ok.any():
        return 1.0, None
    med = float(np.median(rel[ok]))
    w = np.where(ok, rel / med, 1.0)
    return np.clip(w, 0.25, 4.0), med


def _fit_exponential(h: np.ndarray, sv: np.ndarray, var: float) -> tuple[float, float, float]:
    """Nugget, partial sill and range (km) of an exponential variogram (grid-searched range)."""
    edges = np.quantile(h, np.linspace(0, 1, 9))
    centers, gam = [], []
    for lo, hi in itertools.pairwise(edges):
        m = (h >= lo) & (h <= hi)
        if m.sum() >= 3:
            centers.append(h[m].mean())
            gam.append(sv[m].mean())
    if len(centers) < 3:
        return 0.2 * var, 0.8 * var, DEFAULT_CORR_LENGTH_KM
    centers_a, gam_a = np.array(centers), np.array(gam)
    best = None
    for a_try in np.geomspace(1.0, 500.0, 60):
        basis = np.column_stack([np.ones_like(centers_a), 1.0 - np.exp(-centers_a / a_try)])
        coef, *_ = np.linalg.lstsq(basis, gam_a, rcond=None)
        coef = np.clip(coef, [0.0, 1e-9], [10 * var, 10 * var])
        err = float(np.sum((basis @ coef - gam_a) ** 2))
        if best is None or err < best[0]:
            best = (err, float(coef[0]), float(coef[1]), float(a_try))
    assert best is not None
    return best[1], best[2], best[3]


def default_model(
    result: Any,
    *,
    field_name: str = "accumulation",
    sigma_ln: float | None = None,
    corr_length_km: float | None = None,
    shares: tuple[float, float, float] | None = None,
    scale_by_uncertainty: bool = True,
) -> ErrorModel:
    """The model without gauges: ``sigma`` from the engine's uncertainty model, the rest assumed."""
    notes: list[str] = []
    src: dict[str, str] = {}
    _, med = _weights(result, field_name, True)
    if sigma_ln is not None:
        s, src["sigma_ln"] = float(sigma_ln), "given by the user"
    elif med is not None:
        s = med
        src["sigma_ln"] = (
            "the engine's own uncertainty model (median relative 1 sigma of the accumulation where "
            "at least 0.5 mm, as a log standard deviation); not fitted to gauges"
        )
    else:
        s = 0.3
        src["sigma_ln"] = "ASSUMED DEFAULT 0.3 (no uncertainty field with rain available)"
        notes.append("No cell has an uncertainty estimate with rain: sigma 0.3 is an assumption.")
    if corr_length_km is not None:
        length, src["corr_length_km"] = float(corr_length_km), "given by the user"
    else:
        length, src["corr_length_km"] = (
            DEFAULT_CORR_LENGTH_KM,
            "ASSUMED DEFAULT (not measured, not from the literature): choose it from the gauge "
            "spacing or a radar-gauge comparison",
        )
    if shares is not None:
        sh, src["shares"] = tuple(float(v) for v in shares), "given by the user"
    else:
        sh, src["shares"] = (
            DEFAULT_SHARES,
            "ASSUMED DEFAULT (not measured): 30 % common to the domain, 50 % spatially "
            "correlated, 20 % cell to cell",
        )
    model = ErrorModel(s, sh, length, scale_by_uncertainty, src, None, notes)  # type: ignore[arg-type]
    model.validate()
    return model


def model_from_gauges(
    result: Any,
    pairs: Any,
    *,
    field_name: str = "accumulation",
    min_amount: float = 1.0,
    scale_by_uncertainty: bool = True,
    sigma_ln: float | None = None,
    corr_length_km: float | None = None,
    shares: tuple[float, float, float] | None = None,
) -> ErrorModel:
    """The model with parameters from gauge-radar pairs (``engine.hydro.gauges.Pairs`` of the
    radar-only accumulation). Falls back to :func:`default_model` for what the gauges cannot give,
    and says so."""
    g = np.asarray(pairs.gauge, dtype=float)
    r = np.asarray(pairs.radar, dtype=float)
    ok = np.isfinite(g) & np.isfinite(r) & (g >= min_amount) & (r >= min_amount)
    n = int(ok.sum())
    base = default_model(
        result,
        field_name=field_name,
        sigma_ln=sigma_ln,
        corr_length_km=corr_length_km,
        shares=shares,
        scale_by_uncertainty=scale_by_uncertainty,
    )
    if n < MIN_GAUGES:
        base.notes.append(
            f"Only {n} gauge(s) with at least {min_amount:g} mm on both sides (need {MIN_GAUGES}): "
            "the error model is NOT taken from gauges."
        )
        base.gauge_stats = {"n_pairs": n, "used": False}
        return base
    e = np.log(g[ok] / r[ok])
    mu = float(e.mean())
    var = float(e.var())
    rms = math.sqrt(var + mu**2)
    stats: dict[str, Any] = {
        "n_pairs": n,
        "used": True,
        "mean_ln_ratio": mu,
        "mean_ratio_gauge_over_radar": math.exp(mu),
        "std_ln_ratio": math.sqrt(var),
        "rms_ln_ratio": rms,
        "min_amount_mm": min_amount,
        "in_sample_note": "estimated from the same gauges the probabilities can be checked "
        "against; use independent gauges for the reliability check",
    }
    fit = None
    fit_fn = _radarx("qpe_error_model")
    if fit_fn is not None:
        fit = fit_fn(
            r[ok], g[ok], x=np.asarray(pairs.x)[ok], y=np.asarray(pairs.y)[ok], min_value=min_amount
        )
    sources = dict(base.sources)
    sh, length = base.shares, base.corr_length_km
    if sigma_ln is None:
        base.sigma_ln = max(rms, 1e-3)
        sources["sigma_ln"] = (
            f"gauges: root mean square of ln(gauge/radar) over {n} pairs (includes the bias)"
        )
    if shares is None:
        common = mu**2 / (var + mu**2) if (var + mu**2) > 0 else 0.0
        rest = 1.0 - common
        if n >= MIN_GAUGES_VARIOGRAM:
            if fit is not None and math.isfinite(float(fit.correlation_length)):
                a = float(fit.correlation_length) / 1000.0
                sh = (float(fit.share_common), float(fit.share_spatial), float(fit.share_nugget))
                stats["variogram"] = {"model": "exponential", "range_km": a, "fitted_by": "radarx"}
            else:
                res = e - mu
                x, y = np.asarray(pairs.x)[ok], np.asarray(pairs.y)[ok]
                d = np.hypot(x[:, None] - x[None, :], y[:, None] - y[None, :]) / 1000.0
                iu = np.triu_indices(n, 1)
                h, sv = d[iu], 0.5 * (res[:, None] - res[None, :])[iu] ** 2
                c0, c, a = _fit_exponential(h, sv, max(var, 1e-6))
                nug = c0 / (c0 + c)
                sh = (common, rest * (1 - nug), rest * nug)
                stats["variogram"] = {
                    "model": "exponential",
                    "nugget": c0,
                    "partial_sill": c,
                    "range_km": a,
                }
            sources["shares"] = (
                f"gauges: bias share from the mean ln ratio, the rest split by an exponential "
                f"variogram of the deviations ({n} pairs)"
            )
            if corr_length_km is None:
                length = a
                sources["corr_length_km"] = (
                    f"gauges: range of the exponential variogram ({n} pairs; with so few gauges "
                    "this is a rough number)"
                )
        else:
            sh = (common, rest * 0.7, rest * 0.3)
            sources["shares"] = (
                f"bias share from {n} gauges; the split of the rest is ASSUMED (70 % spatial, 30 % "
                f"cell to cell): fewer than {MIN_GAUGES_VARIOGRAM} gauges for a variogram"
            )
    base.shares, base.corr_length_km, base.sources = sh, length, sources
    base.gauge_stats = stats
    if abs(mu) > 0.1:
        base.notes.append(
            f"The gauges are on average {100 * (math.exp(mu) - 1):+.0f} % from the radar: the "
            "ensemble is centred on the estimate you gave it; adjust to the gauges first "
            "(--adjust) if you want it centred there."
        )
    base.validate()
    return base


# ---------------------------------------------------------------------------
# Spatially correlated Gaussian fields (circulant embedding)
# ---------------------------------------------------------------------------


class CorrelatedField:
    """Unit-variance Gaussian fields with covariance ``exp(-h / L)`` on a regular grid.

    Circulant embedding (Dietrich and Newsam 1997): the covariance on a torus twice the size of
    the grid is diagonalised by an FFT; negative eigenvalues (they are small for this kernel)
    are set to zero and the variance is renormalised to 1.
    """

    def __init__(self, shape: tuple[int, int], length_cells: float) -> None:
        ny, nx = shape
        self.shape = shape
        my, mx = 2 * ny, 2 * nx
        iy = np.minimum(np.arange(my), my - np.arange(my))
        ix = np.minimum(np.arange(mx), mx - np.arange(mx))
        h = np.hypot(iy[:, None], ix[None, :])
        cov = np.exp(-h / max(length_cells, 1e-6))
        lam = np.fft.fft2(cov).real
        self.negative_share = float(-lam[lam < 0].sum() / max(lam[lam > 0].sum(), 1e-12))
        lam = np.clip(lam, 0.0, None)
        self._amp = np.sqrt(lam / (my * mx))
        self._norm = float(math.sqrt(lam.sum() / (my * mx)))  # sqrt of the variance (~1)
        self._m = (my, mx)

    def sample(self, rng: np.random.Generator) -> np.ndarray:
        my, mx = self._m
        w = rng.standard_normal((my, mx)) + 1j * rng.standard_normal((my, mx))
        x = np.fft.fft2(self._amp * w).real
        ny, nx = self.shape
        return (x[:ny, :nx] / self._norm).astype(np.float32)


# ---------------------------------------------------------------------------
# The ensemble
# ---------------------------------------------------------------------------


@dataclass
class EnsembleResult:
    """Members and the products computed from them."""

    model: ErrorModel
    n_members: int
    seed: int
    base_field: str
    thresholds: tuple[float, ...]
    quantiles: tuple[float, ...]
    arrays: dict[str, np.ndarray]  # product name -> (ny, nx)
    basins: list[dict[str, Any]]
    members: np.ndarray | None  # (n, ny, nx) float32, or None after release
    period: tuple[str, str]
    inputs: list[str]
    software: str
    created: str
    reliability: dict[str, Any] | None = None

    # -- names --------------------------------------------------------------------------------
    @staticmethod
    def prob_name(t: float) -> str:
        return f"prob_ge_{t:g}mm"

    @staticmethod
    def quantile_name(q: float) -> str:
        return f"ens_p{round(100 * q):02d}"

    def provenance(self) -> dict[str, Any]:
        return {
            "type": "RETRIEVED",
            "kind": "ensemble of the accumulation under an assumed error model",
            "disclaimer": DISCLAIMER,
            "quantity": "accumulated precipitation (liquid-water equivalent)",
            "units": "mm (probabilities: 1)",
            "base_field": self.base_field,
            "n_members": self.n_members,
            "seed": self.seed,
            "seed_rule": "member k uses numpy SeedSequence(seed).spawn(n_members)[k]",
            "error_model": self.model.to_dict(),
            "thresholds_mm": list(self.thresholds),
            "quantiles": list(self.quantiles),
            "sampling_error_of_a_probability": (
                f"about sqrt(p (1 - p) / {self.n_members}) = "
                f"{0.5 / math.sqrt(self.n_members):.3f} at "
                "p = 0.5 (Monte Carlo noise of this many members)"
            ),
            "period": list(self.period),
            "inputs": list(self.inputs),
            "software": self.software,
            "created": self.created,
        }

    def record(self) -> dict[str, Any]:
        """Everything needed to regenerate the ensemble from the same estimate (a run record)."""
        return {
            "record_version": 1,
            "type": "qpe_ensemble",
            "n_members": self.n_members,
            "seed": self.seed,
            "base_field": self.base_field,
            "error_model": self.model.to_dict(),
            "thresholds_mm": list(self.thresholds),
            "quantiles": list(self.quantiles),
            "period": list(self.period),
            "inputs": list(self.inputs),
            "software": self.software,
            "created": self.created,
        }

    def summary(self) -> dict[str, Any]:
        d = self.provenance()
        d["basins"] = self.basins
        if self.reliability is not None:
            d["reliability"] = self.reliability
        mean = self.arrays["ens_mean"]
        std = self.arrays["ens_std"]
        m = np.isfinite(mean) & (mean >= 1.0)
        d["median_relative_spread"] = float(np.median(std[m] / mean[m])) if m.any() else None
        d["fields"] = sorted(self.arrays)
        return d


def _check_options(n_members: int, thresholds: Any, quantiles: Any) -> tuple[tuple, tuple]:
    if not isinstance(n_members, (int, np.integer)) or not 2 <= int(n_members) <= 500:
        raise EnsembleError("the number of ensemble members is a whole number from 2 to 500")
    th = tuple(sorted({float(t) for t in thresholds}))
    if any(not math.isfinite(t) or t <= 0 for t in th):
        raise EnsembleError("exceedance thresholds are positive depths in mm")
    qs = tuple(sorted({float(q) for q in quantiles}))
    if any(not 0.0 < q < 1.0 for q in qs):
        raise EnsembleError("quantiles are between 0 and 1 (0.05, 0.5, 0.95)")
    return th, qs


def generate_members(
    base: np.ndarray,
    cell_m: float,
    model: ErrorModel,
    n_members: int,
    seed: int,
    weights: np.ndarray | float = 1.0,
) -> np.ndarray:
    """``(n_members, ny, nx)`` float32 members of the multiplicative lognormal error model.

    Cells with 0 mm or no estimate stay as they are (no rain is created, gaps stay gaps).
    """
    model.validate()
    base = np.asarray(base, dtype=np.float32)
    ny, nx = base.shape
    need = n_members * ny * nx * 4
    if need > MAX_MEMBER_BYTES:
        raise EnsembleError(
            f"{n_members} members of {ny} x {nx} cells need {need / 2**20:.0f} MB (limit "
            f"{MAX_MEMBER_BYTES // 2**20} MB): use fewer members or a coarser grid"
        )
    fn = _radarx("qpe_error_ensemble")
    if fn is not None:
        import xarray as xr

        da = xr.DataArray(
            base,
            dims=("y", "x"),
            coords={"y": np.arange(ny) * cell_m, "x": np.arange(nx) * cell_m},
            attrs={"units": "mm"},
        )
        scale = None if np.ndim(weights) == 0 else np.asarray(weights, dtype=np.float32)
        out = fn(
            da,
            sigma_ln=model.sigma_ln,
            correlation_length=model.corr_length_km * 1000.0,
            shares=model.shares,
            members=n_members,
            seed=int(seed),
            scale=scale,
        )
        return out.values
    c, s, n = (math.sqrt(v) for v in model.shares)
    gen = CorrelatedField((ny, nx), model.corr_length_km * 1000.0 / cell_m) if s > 0 else None
    sig = np.float32(model.sigma_ln) * np.asarray(weights, dtype=np.float32)
    half = np.float32(0.5) * sig * sig
    out = np.empty((n_members, ny, nx), dtype=np.float32)
    children = np.random.SeedSequence(int(seed)).spawn(n_members)
    for k, child in enumerate(children):
        rng = np.random.default_rng(child)
        z = np.float32(c * rng.standard_normal())
        if gen is not None:
            z = z + np.float32(s) * gen.sample(rng)
        if n > 0:
            z = z + np.float32(n) * rng.standard_normal((ny, nx)).astype(np.float32)
        out[k] = base * np.exp(sig * z - half)
    return out


def _exceedance(members: np.ndarray, t: float) -> np.ndarray:
    """Share of the members at or above ``t`` (radarx ``exceedance_probability`` when available)."""
    fn = _radarx("exceedance_probability")
    if fn is not None:
        import xarray as xr

        return fn(xr.DataArray(members, dims=("member", "y", "x")), t).values.astype(np.float64)
    valid = np.isfinite(members)
    count = valid.sum(axis=0)
    hits = ((members >= t) & valid).sum(axis=0)
    return np.where(count > 0, hits / np.maximum(count, 1), np.nan)


def _basin_ensembles(
    members: np.ndarray,
    basin_weights: list[Any],
    thresholds: tuple[float, ...],
    qs: tuple[float, ...],
) -> list[dict[str, Any]]:
    flat = members.reshape(members.shape[0], -1)
    rows = []
    for bw in basin_weights:
        v = flat[:, bw.flat].astype(np.float64)
        w = bw.area_km2
        ok = np.isfinite(v)
        wsum = np.where(ok, w[None, :], 0.0).sum(axis=1)
        mean = np.where(
            wsum > 0,
            np.where(ok, v * w[None, :], 0.0).sum(axis=1) / np.maximum(wsum, 1e-12),
            np.nan,
        )
        good = mean[np.isfinite(mean)]
        row: dict[str, Any] = {
            "id": bw.basin.id,
            "name": bw.basin.name,
            "area_km2": bw.area_total_km2,
        }
        if good.size == 0:
            rows.append(row)
            continue
        row["basin_mean_mm"] = {
            "mean": float(good.mean()),
            "std": float(good.std(ddof=1)) if good.size > 1 else 0.0,
            **{f"p{round(100 * q):02d}": float(np.quantile(good, q)) for q in qs},
        }
        row["prob_basin_mean_ge"] = {f"{t:g}": float(np.mean(good >= t)) for t in thresholds}
        rows.append(row)
    return rows


def generate(
    result: Any,
    model: ErrorModel | None = None,
    *,
    n_members: int = 30,
    seed: int = 0,
    thresholds: Any = DEFAULT_THRESHOLDS,
    quantiles: Any = DEFAULT_QUANTILES,
    field_name: str | None = None,
    keep_members: bool = True,
) -> EnsembleResult:
    """Build the ensemble of a :class:`QpeResult` accumulation and its products.

    ``model`` defaults to :func:`default_model`. ``field_name`` is ``accumulation`` or, when the
    result was adjusted to gauges, ``accumulation_adjusted`` (the default then).
    """
    if result.duration_s <= 0:
        raise EnsembleError(
            "an ensemble of accumulations needs several volumes (a period); one volume gives a rate"
        )
    th, qs = _check_options(n_members, thresholds, quantiles)
    name = field_name or (
        "accumulation_adjusted" if result.accumulation_adjusted is not None else "accumulation"
    )
    base = getattr(result, name, None)
    if base is None:
        raise EnsembleError(f"the result has no field {name!r}")
    model = model or default_model(result, field_name=name)
    w, _ = _weights(result, name, model.scale_by_uncertainty)
    members = generate_members(base, result.grid.cell_m, model, int(n_members), int(seed), w)

    n = members.shape[0]
    arrays: dict[str, np.ndarray] = {}
    valid = np.isfinite(np.asarray(base))
    mean = members.mean(axis=0, dtype=np.float64)
    arrays["ens_mean"] = np.where(valid, mean, np.nan).astype(np.float32)
    arrays["ens_std"] = np.where(
        valid, members.std(axis=0, ddof=1, dtype=np.float64), np.nan
    ).astype(np.float32)
    qv = np.quantile(members, qs, axis=0)
    for q, arr in zip(qs, qv, strict=True):
        arrays[EnsembleResult.quantile_name(q)] = np.where(valid, arr, np.nan).astype(np.float32)
    for t in th:
        p = _exceedance(members, t)
        arrays[EnsembleResult.prob_name(t)] = np.where(valid, p, np.nan).astype(np.float32)
    basin_rows: list[dict[str, Any]] = []
    if result.basin_series is not None:
        basin_rows = _basin_ensembles(members, result.basin_series.weights, th, qs)
    out = EnsembleResult(
        model=model,
        n_members=n,
        seed=int(seed),
        base_field=name,
        thresholds=th,
        quantiles=qs,
        arrays=arrays,
        basins=basin_rows,
        members=members if keep_members else None,
        period=(prov.iso_utc(result.start), prov.iso_utc(result.end)),
        inputs=prov.source_names(result.source_files),
        software=prov.software_string(),
        created=prov.utc_stamp(),
    )
    return out


def regenerate(result: Any, record: dict[str, Any], *, keep_members: bool = True) -> EnsembleResult:
    """Rebuild an ensemble from its run record and the same estimate (bit-identical members)."""
    if record.get("type") != "qpe_ensemble":
        raise EnsembleError("this is not an ensemble run record (type 'qpe_ensemble')")
    return generate(
        result,
        ErrorModel.from_dict(record["error_model"]),
        n_members=int(record["n_members"]),
        seed=int(record["seed"]),
        thresholds=record.get("thresholds_mm", DEFAULT_THRESHOLDS),
        quantiles=record.get("quantiles", DEFAULT_QUANTILES),
        field_name=record.get("base_field"),
        keep_members=keep_members,
    )


# ---------------------------------------------------------------------------
# Verification: reliability against gauges
# ---------------------------------------------------------------------------


def sample_members(
    members: np.ndarray, grid: Any, x: np.ndarray, y: np.ndarray, radius_cells: int = 1
) -> np.ndarray:
    """``(n_members, n_points)``: the mean of the finite cells within ``radius_cells`` of each point
    (the same window the gauge comparison uses)."""
    ix = np.rint((np.asarray(x) - grid.x[0]) / grid.cell_m).astype(int)
    iy = np.rint((np.asarray(y) - grid.y[0]) / grid.cell_m).astype(int)
    n, ny, nx = members.shape
    out = np.full((n, len(ix)), np.nan, dtype=np.float64)
    for k in range(len(ix)):
        if not (0 <= ix[k] < nx and 0 <= iy[k] < ny):
            continue
        j0, j1 = max(iy[k] - radius_cells, 0), min(iy[k] + radius_cells + 1, ny)
        i0, i1 = max(ix[k] - radius_cells, 0), min(ix[k] + radius_cells + 1, nx)
        w = members[:, j0:j1, i0:i1].reshape(n, -1)
        with np.errstate(invalid="ignore"):
            out[:, k] = np.nanmean(w, axis=1)
    return out


def reliability(
    prob: np.ndarray, event: np.ndarray, n_bins: int = 10, min_per_bin: int = 5
) -> dict[str, Any]:
    """Reliability diagram data and Brier score of probabilities ``prob`` against the 0/1 ``event``.

    Bins of equal width in probability; a bin with fewer than ``min_per_bin`` cases is reported
    but not used for the slope. ``reliability_slope`` is the least-squares slope of observed
    frequency on forecast
    probability over the usable bins, weighted by count (1 is perfect, below 1 overconfident).
    Brier skill is against the climatological frequency of the same sample.
    """
    p = np.asarray(prob, dtype=float)
    o = np.asarray(event, dtype=float)
    ok = np.isfinite(p) & np.isfinite(o)
    p, o = p[ok], o[ok]
    n = int(p.size)
    if n == 0:
        raise EnsembleError("no cases for a reliability diagram")
    base = float(o.mean())
    ref = base * (1 - base)
    edges = np.linspace(0, 1, n_bins + 1)
    rel_fn, bs_fn = _radarx("reliability"), _radarx("brier_score")
    bins = []
    if rel_fn is not None and bs_fn is not None:
        import xarray as xr

        pa, oa = xr.DataArray(p), xr.DataArray(o)
        tab = rel_fn(pa, oa, bins=n_bins)
        brier = float(bs_fn(pa, oa))
        for b in range(n_bins):
            cnt = int(tab["count"].values[b])
            mp, of = (
                float(tab["forecast_probability"].values[b]),
                float(tab["observed_frequency"].values[b]),
            )
            bins.append(
                {
                    "lo": float(edges[b]),
                    "hi": float(edges[b + 1]),
                    "n": cnt,
                    "mean_probability": mp if cnt else None,
                    "observed_frequency": of if cnt else None,
                }
            )
    else:
        brier = float(np.mean((p - o) ** 2))
        idx = np.clip(np.digitize(p, edges[1:-1]), 0, n_bins - 1)
        for b in range(n_bins):
            m = idx == b
            bins.append(
                {
                    "lo": float(edges[b]),
                    "hi": float(edges[b + 1]),
                    "n": int(m.sum()),
                    "mean_probability": float(p[m].mean()) if m.any() else None,
                    "observed_frequency": float(o[m].mean()) if m.any() else None,
                }
            )
    use = [b for b in bins if b["n"] >= min_per_bin]
    slope = None
    if len(use) >= 3:
        xs = np.array([b["mean_probability"] for b in use])
        ys = np.array([b["observed_frequency"] for b in use])
        ws = np.array([b["n"] for b in use], dtype=float)
        if np.ptp(xs) > 1e-9:
            xm, ym = np.average(xs, weights=ws), np.average(ys, weights=ws)
            slope = float(np.sum(ws * (xs - xm) * (ys - ym)) / np.sum(ws * (xs - xm) ** 2))
    return {
        "n": n,
        "base_rate": base,
        "brier_score": brier,
        "brier_score_climatology": ref,
        "brier_skill_score": (1.0 - brier / ref) if ref > 0 else None,
        "reliability_slope": slope,
        "bins": bins,
    }


def verify_with_gauges(
    ens: EnsembleResult, result: Any, pairs: Any, *, radius_cells: int = 1
) -> dict[str, Any]:
    """Reliability of the exceedance probabilities at the gauges, for each threshold and pooled.

    The event is "the gauge total is at least t mm", the probability the share of members with at
    least t mm in the cells around the gauge. Needs members (``keep_members``) and at least
    ``MIN_GAUGES_RELIABILITY`` gauges; with fewer it says the probabilities are NOT validated.
    """
    if ens.members is None:
        raise EnsembleError("the members were released: build the ensemble with keep_members=True")
    n = int(np.size(pairs.gauge))
    out: dict[str, Any] = {
        "n_gauges": n,
        "model_from_the_same_gauges": bool(
            ens.model.gauge_stats and ens.model.gauge_stats.get("used")
        ),
    }
    if n < MIN_GAUGES_RELIABILITY:
        out["validated"] = False
        out["note"] = (
            f"{n} gauges are too few for a reliability diagram (need "
            f"{MIN_GAUGES_RELIABILITY}): the "
            "probabilities are NOT validated against observations"
        )
        return out
    vals = sample_members(ens.members, result.grid, pairs.x, pairs.y, radius_cells)
    g = np.asarray(pairs.gauge, dtype=float)
    per, pp, oo = {}, [], []
    for t in ens.thresholds:
        p = np.mean(vals >= t, axis=0)
        ev = (g >= t).astype(float)
        if ev.sum() == 0 or ev.sum() == ev.size:
            per[f"{t:g}"] = {
                "note": "the event never or always happened at these gauges: no diagram"
            }
        else:
            per[f"{t:g}"] = reliability(p, ev)
        pp.append(p)
        oo.append(ev)
    out["per_threshold"] = per
    out["pooled"] = reliability(np.concatenate(pp), np.concatenate(oo))
    out["validated"] = True
    out["note"] = (
        "Reliability of the probabilities at the gauges: slope near 1 and a positive Brier "
        "skill mean "
        "the ensemble spread is about right for these gauges. "
        + (
            "The error model was fitted to the same gauges, so this is optimistic; use "
            "independent ones."
            if out["model_from_the_same_gauges"]
            else ""
        )
    )
    return out


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------


def figure(result: Any, ens: EnsembleResult, threshold_mm: float | None = None):
    """Two maps: the spread (standard deviation of the members, mm) and the probability that the
    accumulation is at least ``threshold_mm`` (default: the middle of the stored thresholds)."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    th = ens.thresholds
    if threshold_mm is None:
        threshold_mm = th[len(th) // 2]
    key = EnsembleResult.prob_name(threshold_mm)
    if key not in ens.arrays:
        raise EnsembleError(
            f"no probability map for {threshold_mm:g} mm (have {', '.join(map(str, th))})"
        )
    g = result.grid
    ext = [
        (g.x[0] - g.cell_m / 2) / 1e3,
        (g.x[-1] + g.cell_m / 2) / 1e3,
        (g.y[0] - g.cell_m / 2) / 1e3,
        (g.y[-1] + g.cell_m / 2) / 1e3,
    ]
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.4), constrained_layout=True)
    std = np.where(ens.arrays["ens_mean"] >= 0.05, ens.arrays["ens_std"], np.nan)
    prob = np.where(ens.arrays["ens_mean"] >= 0.05, ens.arrays[key], np.nan)
    for ax, arr, cmap, vmax, label, ttl in (
        (axes[0], std, "magma", None, "standard deviation of the members [mm]", "Spread"),
        (
            axes[1],
            prob,
            "cividis",
            1.0,
            "probability [1]",
            f"P(accumulation >= {threshold_mm:g} mm)",
        ),
    ):
        cm = plt.get_cmap(cmap).copy()
        cm.set_bad("#eef0f2")
        im = ax.imshow(
            np.ma.masked_invalid(arr),
            origin="lower",
            extent=ext,
            cmap=cm,
            vmin=0,
            vmax=vmax,
            interpolation="nearest",
        )
        fig.colorbar(im, ax=ax, shrink=0.85, pad=0.02).set_label(label)
        ax.set_title(ttl, fontsize=10)
        ax.set_xlabel("km east of the grid origin")
        ax.set_aspect("equal")
    axes[0].set_ylabel("km north of the grid origin")
    fig.suptitle(
        f"{ens.n_members} members, seed {ens.seed}, {ens.period[0]} to {ens.period[1]}   "
        "[RETRIEVED ensemble under an ASSUMED error model; not an observation, not calibrated "
        "unless verified]",
        fontsize=8.5,
    )
    return fig


def reliability_figure(rel: dict[str, Any]):
    """Reliability diagram of :func:`reliability` output (pooled), with the sample size per bin."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(4.6, 4.6), constrained_layout=True)
    ax.plot([0, 1], [0, 1], "k--", lw=1, label="perfect")
    bins = [b for b in rel["bins"] if b["n"] > 0]
    ax.plot(
        [b["mean_probability"] for b in bins],
        [b["observed_frequency"] for b in bins],
        "o-",
        color="#08519c",
        label="ensemble",
    )
    for b in bins:
        ax.annotate(
            str(b["n"]),
            (b["mean_probability"], b["observed_frequency"]),
            fontsize=6,
            xytext=(3, -8),
            textcoords="offset points",
            color="#555",
        )
    ax.set_xlabel("forecast probability")
    ax.set_ylabel("observed frequency")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    s = rel.get("reliability_slope")
    ax.set_title(
        f"Reliability, n = {rel['n']}, Brier skill {rel['brier_skill_score']:.2f}"
        + ("" if s is None else f", slope {s:.2f}"),
        fontsize=9,
    )
    ax.legend(fontsize=7, loc="upper left")
    return fig

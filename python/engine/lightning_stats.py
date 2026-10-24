"""Statistics of an LMA lightning set: height distribution of the sources, flash initiation heights, flash rate and lightning jumps.

All the algorithms are radarx's (``radarx.retrieve``): ``vertical_source_distribution`` (sources and flash
initiations per height bin), ``lightning_jump`` (the "2 sigma" algorithm of Schultz et al. 2009) and, for the
flash rate, ``flash_rate`` (radarx 0.5.4 and later; :func:`engine.lightning_layer.rate_series` is the same
maths for older radarx). This module chooses the inputs, runs them and shapes the answer for the viewer.

Limits, stated in the answer: the height of a source is above mean sea level; the distribution is of
located sources, so it depends on the detection efficiency, which falls with range from the network and with
the number of stations (shallow sources far from the array are missed first); the lightning jump needs a
flash rate of at least ``min_rate`` and 14 minutes of data (six 2-minute periods and the current one). Charge
layers are **not** inferred: that would be an interpretation of the sources, not a measurement, and this
program does not offer it.

    Schultz, C. J., W. A. Petersen, L. D. Carey, 2009: Preliminary development and evaluation of lightning
    jump algorithms for the real-time detection of severe weather. J. Appl. Meteor. Climatol., 48, 2543-2563,
    https://doi.org/10.1175/2009JAMC2237.1
"""

# ruff: noqa: E501
from __future__ import annotations

from typing import Any

import numpy as np

from engine.lightning_layer import LightningSet, rate_series

JUMP_MIN_MINUTES = 14


def _round(a: np.ndarray, n: int = 4) -> list[float | None]:
    return [None if not np.isfinite(v) else round(float(v), n) for v in np.asarray(a, dtype=float)]


def lma_stats(ls: LightningSet, *, z_step_m: float = 500.0, bin_s: float = 60.0) -> dict[str, Any]:
    """Height distribution and lightning jumps of an LMA set read by :func:`engine.lightning_layer.read_lma`."""
    if ls.kind != "lma" or ls.source_ds is None:
        raise ValueError("statistics need LMA sources (GLM has no height)")
    if ls.n_points == 0:
        raise ValueError("there are no sources in the window to count")
    if not 100.0 <= z_step_m <= 5000.0:
        raise ValueError("the height step must be 100 to 5000 m")
    try:
        from radarx.retrieve import lightning_jump, vertical_source_distribution
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ImportError("Lightning statistics need the radarx library.") from exc
    import xarray as xr

    ds = ls.source_ds
    alt = np.asarray(ds["event_altitude"].values, dtype=float)
    lo = np.floor(np.nanmin(alt) / z_step_m) * z_step_m
    hi = np.ceil(np.nanmax(alt) / z_step_m) * z_step_m + z_step_m
    z = (
        np.arange(lo, hi + z_step_m / 2, z_step_m) + z_step_m / 2
    )  # bin centres, m above mean sea level
    times = np.asarray(ds["event_time"].values).astype("datetime64[ns]")
    edges = np.array([times.min(), times.max() + np.timedelta64(1, "ns")], dtype="datetime64[ns]")
    prof = vertical_source_distribution(ds, z, time_edges=edges)
    src = np.asarray(prof["source_count"].values)[0]
    ini = np.asarray(prof["flash_initiation_count"].values)[0]

    rate = rate_series(ls.f_t0, ls.coverage[0][0], ls.coverage[-1][1], bin_s, ls.coverage)
    out: dict[str, Any] = {
        "height": {
            "z_km": _round(z / 1000.0, 4),
            "z_step_m": z_step_m,
            "sources": [int(v) for v in src],
            "initiations": [int(v) for v in ini],
            "reference": "heights are metres above mean sea level",
        },
        "rate": {
            "bin_s": bin_s,
            "edges_s": _round(rate["edges"], 3),
            "per_min": _round(rate["rate_per_min"], 4),
        },
        "jump": {"available": False, "reason": "", "start_s": [], "level": []},
        "notes": [
            "Flash initiations are counted for flashes of at least 10 sources (the radarx default); smaller flashes are not shown as dots.",
            "Heights are of located sources above mean sea level; shallow sources far from the array are missed first, so the distribution depends on the range of the storm from the network.",
            "Charge layers are not inferred: that would be an interpretation of the sources, not a measurement.",
        ],
        "references": [
            "Schultz et al. (2009), J. Appl. Meteor. Climatol. 48, 2543-2563, https://doi.org/10.1175/2009JAMC2237.1"
        ],
    }

    # lightning jump: the 2-sigma algorithm on the flash rate per minute (radarx, Schultz et al. 2009)
    span_min = (ls.coverage[-1][1] - ls.coverage[0][0]) / 60.0
    rate1 = rate_series(ls.f_t0, ls.coverage[0][0], ls.coverage[-1][1], 60.0, ls.coverage)
    covered = np.isfinite(rate1["rate_per_min"])
    if span_min < JUMP_MIN_MINUTES or covered.sum() < JUMP_MIN_MINUTES:
        out["jump"]["reason"] = (
            f"A lightning jump needs at least {JUMP_MIN_MINUTES} minutes of data; {span_min:.1f} minutes are covered."
        )
        return out
    t0 = np.datetime64(round((ls.t_ref.timestamp() + rate1["edges"][0]) * 1e9), "ns")
    tcoord = t0 + (np.arange(len(rate1["rate_per_min"])) * 60e9).astype("timedelta64[ns]")
    fr = xr.DataArray(
        np.nan_to_num(rate1["rate_per_min"], nan=0.0),
        dims="time",
        coords={"time": tcoord},
        name="flash_rate",
    )
    jumps = lightning_jump(fr)
    starts = np.nonzero(np.asarray(jumps["jump_start"].values, dtype=bool))[0]
    # the result is on the averaging periods: their times are the answer, not the one-minute bin edges
    t_ref64 = np.datetime64(round(ls.t_ref.timestamp() * 1e9), "ns")
    start_s = (
        np.asarray(jumps["time"].values).astype("datetime64[ns]")[starts] - t_ref64
    ) / np.timedelta64(1, "s")
    out["jump"] = {
        "available": True,
        "reason": "",
        "start_s": _round(start_s, 3),
        "level": _round(np.asarray(jumps["sigma_level"].values)[starts], 3),
        "sigma": 2.0,
        "min_rate_per_min": 10.0,
        "gaps_set_to_zero": int((~covered).sum()),
    }
    if (~covered).any():
        out["notes"].append(
            f"{int((~covered).sum())} one-minute bins without data were counted as zero for the jump algorithm, which needs a continuous series."
        )
    return out

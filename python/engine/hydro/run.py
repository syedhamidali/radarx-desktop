"""One call that does what ``rxd qpe`` does: accumulate, adjust to gauges, summarise basins.

Used by the command line, the Python API (``api.qpe``), the QPE module and the QPE panel so that all
of them give the same numbers from the same options.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from engine.hydro import qpe as hqpe

ProgressFn = Callable[[int, str], None]

#: options of ``accumulate`` that are not QPE-configuration fields
ACCUMULATE_OPTIONS = (
    "resolution_m",
    "extent",
    "max_range_km",
    "start",
    "end",
    "composite",
    "advect",
    "tile_km",
    "step_s",
    "max_gap_s",
    "hold_edges_s",
    "n_sweeps",
    "max_elev_deg",
)


def split_options(options: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(qpe_config, accumulate_options)`` from flat keyword options; unknown names are errors
    with a suggestion."""
    import difflib

    cfg_fields = set(hqpe.QpeConfig.__dataclass_fields__)
    cfg: dict[str, Any] = {}
    acc: dict[str, Any] = {}
    for key, value in options.items():
        if key in cfg_fields:
            cfg[key] = value
        elif key in ACCUMULATE_OPTIONS:
            acc[key] = value
        else:
            close = difflib.get_close_matches(key, sorted(cfg_fields | set(ACCUMULATE_OPTIONS)), 2)
            hint = f" Did you mean {' or '.join(repr(c) for c in close)}?" if close else ""
            raise hqpe.QpeError(f"qpe: unknown option {key!r}.{hint}")
    return cfg, acc


def run_qpe(
    readers: Sequence[Any],
    *,
    config: Mapping[str, Any] | None = None,
    basins: Any = None,
    basin_options: Mapping[str, Any] | None = None,
    thresholds: Sequence[float] | None = None,
    frequency: Any = None,
    gauges: Any = None,
    gauge_options: Mapping[str, Any] | None = None,
    adjust: str | None = None,
    adjust_options: Mapping[str, Any] | None = None,
    dem: Any = None,
    ensemble: Mapping[str, Any] | None = None,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
    **accumulate_options: Any,
):
    """Accumulate ``readers`` and, when given, summarise ``basins`` and adjust to ``gauges``.

    ``basins`` / ``gauges`` / ``dem`` / ``frequency`` may be file paths or the objects of
    :mod:`engine.hydro.basins` / :mod:`engine.hydro.gauges`. Returns the
    :class:`engine.hydro.accumulate.QpeResult` (``result.basin_series``, ``result.validation``,
    ``result.exceedance``). ``ensemble`` (``{"n_members": 30, "seed": 0, "thresholds": [10, 25],
    "sigma_ln": ..., "corr_length_km": ..., "shares": [c, s, n]}``) adds ``result.ensemble``, the
    ensemble of the accumulation under an assumed error model (:mod:`engine.hydro.ensemble`);
    with gauges the error model is taken from them and the probabilities are checked against them.
    """
    from engine.hydro import accumulate as acc
    from engine.hydro import basins as hb
    from engine.hydro import gauges as hg

    cfg = dict(config or {})
    if dem is not None:
        cfg["dem"] = hqpe.load_dem(str(dem)) if isinstance(dem, (str, Path)) else dem
    bset = None
    if basins is not None:
        bset = (
            basins
            if isinstance(basins, hb.BasinSet)
            else hb.BasinSet.from_file(basins, **dict(basin_options or {}))
        )
    gset = None
    if gauges is not None:
        gset = (
            gauges
            if isinstance(gauges, hg.GaugeSet)
            else hg.read_gauges(gauges, **dict(gauge_options or {}))
        )
    result = acc.accumulate(
        readers, cfg, basins=bset, progress=progress, cancel=cancel, **accumulate_options
    )
    if bset is not None and thresholds:
        result.basin_series.thresholds = tuple(float(t) for t in thresholds)
        result.basin_series.stats = [
            result.basin_series._stats_for(k, result.accumulation, result)
            for k in range(len(bset.basins))
        ]
    if gset is not None:
        if result.duration_s <= 0:
            raise hqpe.QpeError(
                "gauge adjustment needs an accumulation: give several volumes (a period), not one"
            )
        if progress:
            progress(96, "Adjusting to gauges")
        result.validation = hg.adjust(
            result, gset, method=adjust or "mfb", **dict(adjust_options or {})
        )
    if bset is not None and frequency is not None:
        result.exceedance = hb.exceedance(result.basin_series, frequency)
    if ensemble:
        if progress:
            progress(98, "Building the ensemble")
        result.ensemble = build_ensemble(result, ensemble, gset)
    return result


ENSEMBLE_OPTIONS = (
    "n_members",
    "seed",
    "thresholds",
    "quantiles",
    "sigma_ln",
    "corr_length_km",
    "shares",
)


def build_ensemble(result: Any, options: Mapping[str, Any], gset: Any = None):
    """The ensemble of ``result`` from ``options`` (see :func:`run_qpe`), with the error model taken
    from the gauges ``gset`` when there are some, and a reliability check against them."""
    from engine.hydro import ensemble as he
    from engine.hydro import gauges as hg

    opts = dict(options)
    unknown = sorted(set(opts) - set(ENSEMBLE_OPTIONS))
    if unknown:
        raise he.EnsembleError(
            f"unknown ensemble option(s) {', '.join(unknown)}; the options are "
            f"{', '.join(ENSEMBLE_OPTIONS)}"
        )
    shares = opts.get("shares")
    shares = tuple(float(v) for v in shares) if shares is not None else None
    if shares is not None and len(shares) != 3:
        raise he.EnsembleError("shares are three numbers: common, spatial, cell to cell")
    name = "accumulation_adjusted" if result.accumulation_adjusted is not None else "accumulation"
    model_kw = {
        "field_name": name,
        "sigma_ln": opts.get("sigma_ln"),
        "corr_length_km": opts.get("corr_length_km"),
        "shares": shares,
    }
    pairs = None
    if gset is not None:
        # errors of the radar-only estimate against the gauges (the adjusted field is exact at the
        # gauges, so its in-sample residuals say nothing about the error)
        totals = gset.quality_control().totals(
            result.start, result.end, min_coverage=0.9, tolerance_s=300.0
        )
        if not totals.empty:
            pairs = hg.pair_with_radar(totals, result, field="accumulation")
    model = (
        he.model_from_gauges(result, pairs, **model_kw)
        if pairs is not None
        else he.default_model(result, **model_kw)
    )
    kw: dict[str, Any] = {}
    for key in ("thresholds", "quantiles"):
        if opts.get(key) is not None:
            kw[key] = opts[key]
    ens = he.generate(
        result,
        model,
        n_members=int(opts.get("n_members", 30)),
        seed=int(opts.get("seed", 0)),
        field_name=name,
        **kw,
    )
    if pairs is not None:
        ens.reliability = he.verify_with_gauges(ens, result, pairs)
    ens.members = None  # the products are kept; the members are not (memory)
    return ens

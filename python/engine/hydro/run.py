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
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
    **accumulate_options: Any,
):
    """Accumulate ``readers`` and, when given, summarise ``basins`` and adjust to ``gauges``.

    ``basins`` / ``gauges`` / ``dem`` / ``frequency`` may be file paths or the objects of
    :mod:`engine.hydro.basins` / :mod:`engine.hydro.gauges`. Returns the
    :class:`engine.hydro.accumulate.QpeResult` (``result.basin_series``, ``result.validation``,
    ``result.exceedance``).
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
    return result

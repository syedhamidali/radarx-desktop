"""The ``rxd indicators`` command (registered by :mod:`engine.hydro.cli`).

Flash-flood indicators from the radar rainfall estimate and the guidance, return-period and
climatology
files you supply (``docs/HYDROLOGY.md``). Experimental guidance, not an official warning.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

EXAMPLES = (
    "examples:\n"
    "  rxd indicators -i 'event/KHPX/*_V06' --start 2021-12-11T02:30 --end 2021-12-11T03:30 \\\n"
    "      --guidance ffg_1h.nc --vintage 'FFG issued 2021-12-11 00Z' -o ind.nc -p ind.png\n"
    "  rxd indicators -i 'event/KHPX/*_V06' --duration-h 3 --frequency-grid 2=atlas_2y.tif \\\n"
    "      --frequency-grid 10=atlas_10y.tif --frequency-grid 100=atlas_100y.tif \\\n"
    "      --frequency-units in\n"
    "  rxd indicators -i 'event/KHPX/*_V06' --climatology p995_1h.nc --json\n"
    "\n"
    "Compares the rainfall of the period (several volumes; the period must be as long as the\n"
    "guidance duration)\n"
    "with guidance you supply (ratio rain / guidance), with precipitation-frequency grids (return\n"
    "period) and with the cell's own climatology (1-h rain relative to its 99.5th percentile, the\n"
    "reference rule of the flash-flood lab). Files are NetCDF, GeoTIFF or lat,lon,value CSV (see\n"
    "docs/HYDROLOGY.md); nothing is downloaded. Experimental guidance, not an official warning."
)


def register(command: Any, cli: Any) -> None:
    from engine.hydro import cli as hcli

    p = command(
        "indicators",
        "Flash-flood indicators: rain vs guidance, return period, relative to climatology.",
        EXAMPLES,
    )
    cli._add_input(p, "radar volumes (several in time = accumulation)")
    cli._add_output(p, "result: .nc (CF NetCDF) / .json (summary) (repeatable)")
    cli._add_plot(p, "also write one map per indicator (<name>_<indicator>.png/.pdf/.svg)")
    cli._add_param(p, "any QPE option KEY=VALUE (repeatable)")
    hcli._estimator_options(p)
    hcli._grid_time_options(p)
    hcli._gauge_options(p)
    hcli._basin_options(p, required=False)
    g = p.add_argument_group("indicator inputs (your files; nothing is downloaded)")
    g.add_argument(
        "--duration-h",
        type=float,
        default=1.0,
        help="duration of the guidance / frequency depths in hours (default 1); the rainfall "
        "period must be this long (shorter gives lower bounds, longer is refused)",
    )
    g.add_argument(
        "--guidance", metavar="FILE", help="flash-flood guidance depth grid (rain/guidance)"
    )
    g.add_argument(
        "--guidance-units", metavar="U", help="mm | in | cm | m (default: the file's, else mm)"
    )
    g.add_argument("--guidance-variable", metavar="NAME", help="NetCDF variable (if several)")
    g.add_argument(
        "--frequency-grid",
        action="append",
        default=[],
        metavar="YEARS=FILE",
        help="depth for that return period, e.g. 10=atlas_10y.tif (repeat, at least two)",
    )
    g.add_argument("--frequency-units", metavar="U", help="units of the frequency grids")
    g.add_argument("--frequency-variable", metavar="NAME", help="NetCDF variable of the grids")
    g.add_argument(
        "--climatology",
        metavar="FILE",
        help="per-cell reference depth (e.g. 99.5th pct of 1-h rain)",
    )
    g.add_argument("--climatology-units", metavar="U", help="units of the climatology grid")
    g.add_argument("--climatology-variable", metavar="NAME", help="NetCDF variable")
    g.add_argument(
        "--vintage",
        metavar="TEXT",
        help="your statement of the data vintage (carried into every output)",
    )
    g.add_argument(
        "--max-distance-km",
        type=float,
        default=25.0,
        help="CSV points: nearest point within this (25)",
    )
    p.add_argument("--dpi", type=int, default=150, help="figure resolution (default 150)")
    p.set_defaults(func=cmd_indicators)


def _frequency_pairs(items: list[str], cl: Any) -> list[tuple[float, str]]:
    out = []
    for it in items:
        years, sep, path = it.partition("=")
        try:
            y = float(years)
        except ValueError:
            y = float("nan")
        if not sep or not path or not y > 0:
            raise cl.UsageError(
                f"--frequency-grid {it!r}: use YEARS=FILE, for example 10=atlas_10y.tif"
            )
        out.append((y, path))
    return out


def cmd_indicators(args: argparse.Namespace, out: Any) -> int:
    import sys

    cl = sys.modules["engine.cli"]
    from engine import api
    from engine.api_results import save_figure
    from engine.hydro import cli as hcli
    from engine.hydro import indicators as hi

    if not (args.guidance or args.frequency_grid or args.climatology):
        raise cl.UsageError("give at least one of --guidance, --frequency-grid, --climatology")
    for target in args.output:
        if Path(target).suffix.lower() not in (".nc", ".json"):
            raise cl.UsageError(f"-o {target!r}: use .nc or .json")
    freq = _frequency_pairs(args.frequency_grid, cl)
    if freq and len(freq) < 2:
        raise cl.UsageError("--frequency-grid needs at least two return periods")
    files = cl._inputs(args)
    kw = hcli._collect(args, cl)
    res = api.qpe(files, progress=out.progress, **kw)
    out.progress_done()
    ind = api.flood_indicators(
        res,
        duration_h=args.duration_h,
        guidance=args.guidance,
        guidance_units=args.guidance_units,
        guidance_variable=args.guidance_variable,
        frequency=freq or None,
        frequency_units=args.frequency_units,
        frequency_variable=args.frequency_variable,
        climatology=args.climatology,
        climatology_units=args.climatology_units,
        climatology_variable=args.climatology_variable,
        vintage=args.vintage,
        max_distance_km=args.max_distance_km,
    )
    outputs, plot = list(args.output), args.plot
    if not outputs and not plot:
        tag = cl._time_tag(ind.period[1])
        outputs, plot = [f"indicators_{tag}.nc"], f"indicators_{tag}.png"
        out.chose(
            f"{outputs[0]} and one map per indicator ({plot[:-4]}_<indicator>.png) as the outputs "
            "(add -o / -p to choose)",
            "-o",
            outputs[0],
            "-p",
            plot,
        )
    summary = ind.summary()
    if not out.json and not out.quiet:
        _print(ind, summary, cl)
    import json

    for target in outputs:
        path = Path(target).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == ".json":
            from engine.hydro.products import _jsonable

            path.write_text(json.dumps(_jsonable(summary), indent=2), encoding="utf-8")
        else:
            hi.to_dataset(ind).to_netcdf(path, engine="netcdf4", format="NETCDF4")
        out.wrote(path)
    if plot:
        base = Path(plot).expanduser()
        for name in ind.layers:
            out.wrote(
                save_figure(
                    hi.figure(ind, name),
                    base.with_name(f"{base.stem}_{name}{base.suffix}"),
                    args.dpi,
                )
            )
    if out.json:
        from engine.hydro.products import _jsonable

        payload = _jsonable(summary)
        payload["files"] = out.files
        out.emit_json(payload)
    return cl.EXIT_OK


def _print(ind: Any, summary: dict[str, Any], cl: Any) -> None:
    print(
        cl.bold(f"flash-flood indicators ({summary['label']})")
        + f"  |  {ind.period[0]} to {ind.period[1]}  |  {ind.duration_h:g} h"
    )
    print(f"  label    {summary['product_text']}")
    print(f"  limits   {summary['not_modelled']}")
    for lay in summary["layers"].values():
        rows = [
            (label, f"{c['cells']:,}", f"{c['area_km2']:,.0f}")
            for label, c in lay["classes"].items()
            if c["cells"]
        ]
        top = "-" if lay["max"] is None else f"{lay['max']:.2f}"
        print(f"  {lay['label']} [{lay['units']}]  max {top}")
        print(cl._table(("class", "cells", "area km2"), rows, "    "))
        pv = lay["provenance"]
        for k in ("guidance", "frequency", "climatology"):
            if pv.get(k):
                print(
                    f"    input {k}: {pv[k].get('file')}; "
                    f"vintage: {pv[k].get('vintage', 'not given')}"
                )
    for n in summary["notes"]:
        print(f"  note: {n}")

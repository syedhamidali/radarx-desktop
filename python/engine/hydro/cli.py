"""The ``rxd qpe`` and ``rxd basin`` commands (registered into :mod:`engine.cli`).

Kept in this package so the hydrology tools do not touch the shared command line code beyond one
registration call. Importing this module is light (argparse only); everything else is imported by
the command handlers.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

SAMPLE_GLOB = "'mayfield/KHPX/*_V06'"
EXAMPLES_QPE = (
    "examples:\n"
    f"  rxd qpe -i {SAMPLE_GLOB} -o rain.nc -p rain.png\n"
    f"  rxd qpe -i {SAMPLE_GLOB} --relation tropical --band S -o rain_tropical.nc\n"
    f"  rxd qpe -i {SAMPLE_GLOB} --method hybrid --freezing-level-m 3500 -o rain.nc\n"
    "  rxd qpe -i 'mayfield/*/*_V06' --composite best --resolution-m 2000 -o network.nc\n"
    f"  rxd qpe -i {SAMPLE_GLOB} --gauges gauges.csv --adjust ked -o rain_adjusted.nc -v\n"
    f"  rxd qpe -i {SAMPLE_GLOB} --basins basins.geojson -o basins.csv --json\n"
    "  rxd qpe --list-relations\n"
    "\n"
    "Several volumes (of one radar or of several) in time are integrated into a rainfall\n"
    "accumulation (mm) with advection-corrected interpolation between scans; one volume per radar\n"
    "gives the instantaneous rain rate. -o: .nc (CF NetCDF), .png (+ world file), .json\n"
    "(summary, provenance), .csv (basin table). Everything is labelled RETRIEVED (a radar\n"
    "estimate) or ADJUSTED (after gauges). -P passes any option by name (docs/HYDROLOGY.md)."
)
EXAMPLES_BASIN = (
    "examples:\n"
    f"  rxd basin -i {SAMPLE_GLOB} -b watersheds.geojson\n"
    f"  rxd basin -i {SAMPLE_GLOB} -b huc12.shp --basin-id HUC12 -o table.csv -p series.png\n"
    f"  rxd basin -i {SAMPLE_GLOB} -b creek.gpkg --thresholds 10,25,50 --frequency freq_6h.json\n"
    f"  rxd basin -i {SAMPLE_GLOB} -b creek.geojson --gauges gauges.csv --adjust mfb --json\n"
    "\n"
    "Area-weighted rainfall over each basin: the cell fractions inside the polygon are computed\n"
    "exactly (not by counting pixels) and weighted with the true cell area. Output: the basin\n"
    "table (-o .csv/.json; the series of every basin goes to <name>_<id>_series.csv) and a figure\n"
    "with the hyetograph and the cumulative depth (-p). --frequency FILE adds the return period\n"
    "of the basin mean depth from a precipitation-frequency table you supply (never downloaded)."
)


def register(command: Any, cli: Any) -> None:
    """Add ``qpe`` and ``basin`` to the ``rxd`` parser (called by ``engine.cli.build_parser``)."""
    p = command(
        "qpe",
        "Rain rate and rainfall accumulation from radar volumes, with uncertainty (needs radarx).",
        EXAMPLES_QPE,
    )
    cli._add_input(p, "radar volumes (several in time = accumulation)")
    cli._add_output(p, "result: .nc / .png / .json / .csv (repeatable)")
    cli._add_plot(p, "also write a map figure (.png/.pdf/.svg)")
    cli._add_param(p, "any QPE option KEY=VALUE (repeatable), e.g. -P zr_a=200 -P zr_b=1.6")
    _estimator_options(p)
    _grid_time_options(p)
    _gauge_options(p)
    _basin_options(p, required=False)
    _ensemble_options(p)
    p.add_argument(
        "--field",
        metavar="NAME",
        help="field of the picture (default accumulation; with --ensemble also ens_mean, ens_std, "
        "ens_p05, ens_p50, ens_p95, prob_ge_<mm>mm)",
    )
    p.add_argument("--dpi", type=int, default=150, help="figure resolution (default 150)")
    p.add_argument("--list-relations", action="store_true", help="list the Z-R relations and exit")
    p.set_defaults(func=cmd_qpe)

    p = command(
        "basin",
        "Area-weighted rainfall over basins: table, hyetograph, thresholds, return period.",
        EXAMPLES_BASIN,
    )
    cli._add_input(p, "radar volumes (several in time)")
    cli._add_output(p, "basin table .csv / .json / .nc (repeatable)")
    cli._add_plot(p, "also write the rainfall series figure (.png/.pdf/.svg)")
    cli._add_param(p, "any QPE option KEY=VALUE (repeatable)")
    _estimator_options(p)
    _grid_time_options(p)
    _gauge_options(p)
    _basin_options(p, required=True)
    _ensemble_options(p)
    p.add_argument(
        "--bin-minutes", type=float, default=60.0, help="hyetograph bin in minutes (default 60)"
    )
    p.add_argument("--dpi", type=int, default=150, help="figure resolution (default 150)")
    p.set_defaults(func=cmd_basin)


def _ensemble_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group(
        "probabilistic rainfall (an ensemble under an ASSUMED error model, not a forecast)"
    )
    g.add_argument(
        "--ensemble",
        type=int,
        metavar="N",
        help="N members (2 to 500) of the accumulation with a multiplicative lognormal, spatially "
        "correlated error: mean, spread, percentiles and exceedance probabilities (off by default)",
    )
    g.add_argument("--seed", type=int, default=0, help="random seed of the ensemble (default 0)")
    g.add_argument(
        "--exceed",
        metavar="MM,MM",
        help="depths for the exceedance probability maps (default 5,10,25,50,100 mm)",
    )
    g.add_argument(
        "--error-sigma-ln",
        type=float,
        metavar="S",
        help="standard deviation of ln(truth/estimate); default: from the gauges if given, else "
        "from the engine's uncertainty model (0.3 is about 30 %%)",
    )
    g.add_argument(
        "--error-corr-km",
        type=float,
        metavar="KM",
        help="correlation length of the error; default: from the gauges (8 or more), else an "
        "ASSUMED 25 km",
    )
    g.add_argument(
        "--error-shares",
        metavar="C,S,N",
        help="variance shares: common to the domain, spatially correlated, cell to cell "
        "(add up to 1); default 0.3,0.5,0.2 ASSUMED when there are no gauges",
    )


def _estimator_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("estimator")
    g.add_argument(
        "--method",
        default=None,
        metavar="M",
        help="auto (default) | hybrid | z | kdp | z_zdr | a (auto = hybrid if ZDR, phase exist)",
    )
    g.add_argument(
        "--relation",
        default=None,
        metavar="R",
        help="Z-R relation: wsr88d (default), marshall_palmer, tropical, stratiform_east, "
        "stratiform_west, snow_sekhon_srivastava ... (--list-relations)",
    )
    g.add_argument("--band", default=None, metavar="S|C|X", help="radar band (default S)")
    g.add_argument("--precip", default=None, metavar="rain|snow", help="precipitation type")
    g.add_argument(
        "--freezing-level-m",
        type=float,
        default=None,
        metavar="M",
        help="freezing level (m ASL): beams in/above the melting layer give no estimate",
    )
    g.add_argument("--z-cap", type=float, default=None, metavar="DBZ", help="Z cap (default 53)")
    g.add_argument("--no-z-cap", action="store_true", help="do not cap the reflectivity")
    g.add_argument(
        "--dem", metavar="FILE", help="terrain NetCDF: beam blockage and height above ground"
    )
    g.add_argument(
        "--attenuation", default=None, metavar="auto|on|off", help="attenuation correction"
    )


def _grid_time_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("period, grid and compositing")
    g.add_argument(
        "--start", metavar="TIME", help="period start, ISO 8601 UTC (default first scan)"
    )
    g.add_argument("--end", metavar="TIME", help="period end, ISO 8601 UTC (default last scan)")
    g.add_argument("--resolution-m", type=float, default=1000.0, help="grid cell size (1000)")
    g.add_argument("--extent", metavar="W,S,E,N", help="map box in degrees (default: all radars)")
    g.add_argument("--max-range-km", type=float, default=230.0, help="range limit (230)")
    g.add_argument("--composite", default=None, metavar="weighted|best", help="radar compositing")
    g.add_argument("--no-advect", action="store_true", help="linear instead of advected time steps")
    g.add_argument("--step-s", type=float, default=None, help="integration node spacing (60)")
    g.add_argument(
        "--max-gap-min", type=float, default=None, help="longest scan gap bridged, minutes (20)"
    )
    g.add_argument(
        "--hold-edges-s",
        type=float,
        default=None,
        help="hold the first/last scan this long at the period edges (0 = off)",
    )


def _gauge_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("rain gauges (local files; nothing is downloaded)")
    g.add_argument("--gauges", metavar="FILE", help="gauge records: CSV/JSON/NOAA/METAR")
    g.add_argument(
        "--adjust", metavar="M", help="mfb (default) | brandes | ked  (gauge adjustment method)"
    )
    g.add_argument("--gauge-format", metavar="F", help="csv|json|noaa_hourly|noaa_daily|metar")
    g.add_argument("--gauge-units", metavar="U", help="mm | in | 0.01in | 0.1mm ...")
    g.add_argument("--gauge-tz", metavar="TZ", help="time zone of naive times (default UTC)")
    g.add_argument("--gauge-stations", metavar="FILE", help="station table (station, lat, lon)")
    g.add_argument(
        "--gauge-interval-s", type=float, help="accumulation interval of the records (seconds)"
    )
    g.add_argument(
        "--gauge-time", metavar="end|start|centre", help="what the time stamp marks (end)"
    )
    g.add_argument("--no-gauge-qc", action="store_true", help="skip the gauge quality control")
    g.add_argument(
        "--min-gauge-mm",
        type=float,
        default=None,
        help="gauges and radar below this amount are not used to fit the adjustment (1.0)",
    )


def _basin_options(p: argparse.ArgumentParser, required: bool) -> None:
    g = p.add_argument_group("basins")
    g.add_argument(
        "-b",
        "--basins",
        metavar="FILE",
        required=required,
        help="basin polygons: .geojson / .shp / .gpkg",
    )
    g.add_argument("--basin-id", metavar="FIELD", help="attribute used as the basin id")
    g.add_argument("--basin-name", metavar="FIELD", help="attribute used as the basin name")
    g.add_argument("--basin-crs", metavar="CRS", help="coordinate system if the file has none")
    g.add_argument("--thresholds", metavar="MM,MM", help="accumulation thresholds (10,25,50,100)")
    g.add_argument("--frequency", metavar="FILE", help="precipitation-frequency table (JSON)")


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------


def _collect(args: argparse.Namespace, cl: Any) -> dict[str, Any]:
    """Keyword arguments of :func:`engine.api.qpe` from the parsed command line."""
    kw: dict[str, Any] = {k: cl._json_or_text(v) for k, v in cl._split_kv(args.param, "-P").items()}
    for name in (
        "method",
        "relation",
        "band",
        "precip",
        "freezing_level_m",
        "z_cap",
        "attenuation",
    ):
        value = getattr(args, name, None)
        if value is not None:
            kw[name] = value
    if getattr(args, "no_z_cap", False):
        kw["z_cap"] = None
    if getattr(args, "dem", None):
        kw["dem"] = args.dem
    for name in (
        "start",
        "end",
        "resolution_m",
        "max_range_km",
        "composite",
        "step_s",
        "hold_edges_s",
    ):
        value = getattr(args, name, None)
        if value is not None:
            kw[name] = value
    if args.no_advect:
        kw["advect"] = False
    if args.max_gap_min is not None:
        kw["max_gap_s"] = args.max_gap_min * 60.0
    if args.extent:
        kw["extent"] = cl._numbers(args.extent, 4, "--extent")
    if args.basins:
        kw["basins"] = args.basins
        bo = {
            k: v
            for k, v in (
                ("id_field", args.basin_id),
                ("name_field", args.basin_name),
                ("crs", args.basin_crs),
            )
            if v
        }
        if bo:
            kw["basin_options"] = bo
        if args.thresholds:
            kw["thresholds"] = cl._numbers(
                args.thresholds, len(args.thresholds.split(",")), "--thresholds"
            )
        if args.frequency:
            kw["frequency"] = args.frequency
    eo = _ensemble_kwargs(args, cl)
    if eo:
        kw["ensemble"] = eo
    if args.gauges:
        kw["gauges"] = args.gauges
        go = {
            k: v
            for k, v in (
                ("fmt", args.gauge_format),
                ("units", args.gauge_units),
                ("tz", args.gauge_tz),
                ("stations", args.gauge_stations),
                ("interval_s", args.gauge_interval_s),
                ("time_convention", args.gauge_time),
            )
            if v is not None
        }
        if go:
            kw["gauge_options"] = go
        if args.adjust:
            kw["adjust"] = args.adjust
        ao: dict[str, Any] = {}
        if args.no_gauge_qc:
            ao["qc"] = False
        if args.min_gauge_mm is not None:
            ao["min_amount"] = args.min_gauge_mm
        if ao:
            kw["adjust_options"] = ao
    elif args.adjust:
        raise cl.UsageError("--adjust needs --gauges FILE")
    return kw


def _ensemble_kwargs(args: argparse.Namespace, cl: Any) -> dict[str, Any] | None:
    """The ``ensemble`` argument of :func:`engine.api.qpe` from the command line (None = off)."""
    if not hasattr(args, "ensemble"):  # a command without the ensemble options (indicators)
        return None
    given = (args.exceed, args.error_sigma_ln, args.error_corr_km, args.error_shares)
    if not args.ensemble:
        if any(v is not None for v in given):
            raise cl.UsageError("--exceed and --error-* need --ensemble N")
        return None
    eo: dict[str, Any] = {"n_members": args.ensemble, "seed": args.seed}
    if args.exceed:
        eo["thresholds"] = cl._numbers(args.exceed, len(args.exceed.split(",")), "--exceed")
    if args.error_sigma_ln is not None:
        eo["sigma_ln"] = args.error_sigma_ln
    if args.error_corr_km is not None:
        eo["corr_length_km"] = args.error_corr_km
    if args.error_shares:
        eo["shares"] = cl._numbers(args.error_shares, 3, "--error-shares")
    return eo


def cmd_qpe(args: argparse.Namespace, out: Any) -> int:
    import sys

    cl = sys.modules["engine.cli"]
    if args.list_relations:
        return _list_relations(out, cl)
    return _run(args, out, cl, basin_mode=False)


def cmd_basin(args: argparse.Namespace, out: Any) -> int:
    import sys

    return _run(args, out, sys.modules["engine.cli"], basin_mode=True)


def _run(args: argparse.Namespace, out: Any, cl: Any, basin_mode: bool) -> int:
    from engine import api
    from engine.hydro import products

    files = cl._inputs(args)
    for target in args.output:
        ext = Path(target).suffix.lower()
        allowed = (".csv", ".json", ".nc") if basin_mode else (".nc", ".png", ".json", ".csv")
        if ext not in allowed:
            raise cl.UsageError(f"-o {target!r}: use {', '.join(allowed)}")
    kw = _collect(args, cl)
    res = api.qpe(files, progress=out.progress, **kw)
    out.progress_done()
    if not out.json:
        _print_result(res, out, cl, basin_mode)
    outputs, plot = list(args.output), args.plot
    tag = cl._time_tag(_iso(res.end))
    if not outputs and not plot:
        if basin_mode:
            stem = f"basin_{tag}"
            outputs, plot = [f"{stem}.csv"], f"{stem}.png"
            out.chose(
                f"{stem}.csv (+ one series file per basin) and {stem}.png as the outputs "
                "(add -o / -p to choose)",
                "-o",
                outputs[0],
                "-p",
                plot,
            )
        else:
            stem = f"qpe_{tag}"
            outputs, plot = [f"{stem}.nc"], f"{stem}_map.png"
            out.chose(
                f"{stem}.nc and {stem}_map.png as the outputs (add -o / -p to choose)",
                "-o",
                outputs[0],
                "-p",
                plot,
            )
    field = getattr(args, "field", None)
    for target in outputs:
        ext = Path(target).suffix.lower()
        if ext == ".csv" and res.basin_series is None:
            raise cl.UsageError("-o *.csv writes the basin table: give --basins FILE")
        out.wrote(*res.save(target, field=field))
    if plot:
        from engine.api_results import save_figure

        fig = products.series_figure(res, args.bin_minutes) if basin_mode else res.plot(field)
        out.wrote(save_figure(fig, plot, args.dpi))
    if out.json:
        payload = products._jsonable(res.to_dict())
        payload["files"] = out.files
        out.emit_json(payload)
    return cl.EXIT_OK


def _iso(t: Any) -> str:
    from engine import provenance

    return provenance.iso_utc(t)


def _list_relations(out: Any, cl: Any) -> int:
    from engine.hydro import qpe

    rows = [
        (
            r.key,
            r.kind,
            f"Z = {r.A:g} {'S' if r.kind == 'snow' else 'R'}^{r.beta:g}",
            r.label,
        )
        for r in qpe.ZR_RELATIONS.values()
    ]
    if out.json:
        out.emit_json(
            [
                {
                    "key": r.key,
                    "kind": r.kind,
                    "A": r.A,
                    "beta": r.beta,
                    "label": r.label,
                    "reference": r.reference,
                    "note": r.note,
                }
                for r in qpe.ZR_RELATIONS.values()
            ]
        )
        return cl.EXIT_OK
    out.result(cl._table(("relation", "type", "equation", "name"), rows))
    out.result(
        "\ndefault: wsr88d (rain), snow_sekhon_srivastava (snow). Custom: -P zr_a=200 -P zr_b=1.6\n"
        "polarimetric: --method kdp | z_zdr | a | hybrid (coefficients per band, docs/HYDROLOGY.md)"
    )
    return cl.EXIT_OK


def _print_result(res: Any, out: Any, cl: Any, basin_mode: bool) -> None:
    if out.quiet:
        return
    st = res.stats()
    g = res.grid
    ny, nx = g.shape
    minutes = res.duration_s / 60.0
    kind = "instantaneous rain rate" if res.duration_s == 0 else "rainfall accumulation"
    adj = res.accumulation_adjusted is not None
    label = "ADJUSTED" if adj else "RETRIEVED"
    scans = sum(r["scans"] for r in res.radars)
    print(
        cl.bold(kind)
        + f"  |  {len(res.radars)} radar(s), {scans} volume(s)"
        + (f"  |  {_iso(res.start)} to {_iso(res.end)} ({minutes:.1f} min)" if minutes else "")
        + f"  |  {nx} x {ny} cells @ {g.cell_m:g} m"
        + f"  |  {res.timing.get('total_s', 0):.1f} s"
    )
    method = str(res.config.get("method_text") or res.config.get("method"))
    import textwrap

    print(textwrap.fill(method, 100, initial_indent="  method   ", subsequent_indent="           "))
    print(f"  label    {label}: an estimate, not an observation")
    if res.duration_s > 0 and st["max_mm"] is not None:
        a = res.accumulation_adjusted if adj else res.accumulation
        import numpy as np

        fin = np.isfinite(a)
        area = g.cell_areas_km2()
        mean = float(np.average(a[fin], weights=area[fin])) if fin.any() else float("nan")
        print(
            f"  values   max {float(np.nanmax(a)):.1f} mm  mean {mean:.2f} mm over "
            f"{st['area_km2']:,.0f} km2  |  max rate {st['max_rate_mm_h']:.0f} mm/h  |  "
            f"area >= 0.1 mm {st['wet_area_km2']:,.0f} km2"
        )
        sig = res.accumulation_sigma
        both = np.isfinite(sig) & np.isfinite(res.accumulation) & (res.accumulation >= 1.0)
        if both.any():
            rel = float(np.median(sig[both] / res.accumulation[both]))
            print(
                f"  uncertainty  median {100 * rel:.0f} % (1 sigma: relation scatter, "
                "calibration; errors fully correlated in time)"
            )
        if st["mean_valid_fraction"] is not None:
            share = 100 * st["mean_valid_fraction"]
            print(f"  data     {share:.0f} % of the period observed (mean over cells)")
    elif st["max_rate_mm_h"] is not None:
        print(f"  values   max rate {st['max_rate_mm_h']:.0f} mm/h over {st['area_km2']:,.0f} km2")
    rows = [
        (
            r["name"],
            f"{r['latitude']:.3f}",
            f"{r['longitude']:.3f}",
            r["scans"],
            (r["first"] or "-")[11:19],
            (r["last"] or "-")[11:19],
            "-"
            if not r["motion_mean_ms"]
            else f"{r['motion_mean_ms'][0]:+.1f},{r['motion_mean_ms'][1]:+.1f}",
        )
        for r in res.radars
    ]
    print(
        cl._table(("radar", "lat", "lon", "scans", "first", "last", "motion u,v m/s"), rows, "  ")
    )
    bs = res.basin_series
    if bs is not None:
        brow = []
        for s in bs.summary():
            brow.append(
                (
                    s["id"],
                    f"{s['area_km2']:,.0f}",
                    "-"
                    if s.get("accumulation_mean_mm") is None
                    else f"{s['accumulation_mean_mm']:.1f}",
                    "-"
                    if s.get("accumulation_max_mm") is None
                    else f"{s['accumulation_max_mm']:.1f}",
                    "-"
                    if s.get("peak_mean_rate_mm_h") is None
                    else f"{s['peak_mean_rate_mm_h']:.1f}",
                    f"{100 * s['coverage']:.0f}",
                )
            )
        print(
            cl._table(
                ("basin", "area km2", "mean mm", "max mm", "peak mean mm/h", "coverage %"),
                brow,
                "  ",
            )
        )
        thr = bs.thresholds
        for s in bs.summary()[:8]:
            parts = ", ".join(f">={t:g} mm {s[f'area_acc_ge_{t:g}mm_km2']:,.0f} km2" for t in thr)
            print(f"  {s['id']}: area {parts}")
    if res.exceedance:
        print(
            cl._table(
                ("basin", "duration h", "max mean depth mm", "return period yr", "note"),
                [
                    (
                        e["id"],
                        f"{e['duration_h']:g}",
                        f"{e['max_depth_mm']:.1f}",
                        _rp(e),
                        e["flag"] or e["note"],
                    )
                    for e in res.exceedance
                ],
                "  ",
            )
        )
    if res.validation:
        v = res.validation
        before, after = (
            v["validation_before"]["overall"],
            v["validation_after_in_sample"]["overall"],
        )
        print(
            f"  gauges   {v['pairs']} used ({v['qc'].get('good', 0)} good records)  |  "
            f"{res.adjusted['method']}"
        )
        rows = [
            ("radar (before)", *_m(before)),
            ("adjusted (in-sample)", *_m(after)),
        ]
        cv = v.get("cross_validation")
        if cv:
            rows.append(("adjusted (leave-one-out)", *_m(cv["adjusted"])))
        print(cl._table(("", "n", "rel. bias", "RMSE mm", "corr", "KGE"), rows, "  "))
        if v.get("window_mismatch_s", 0) > 60:
            import sys

            print(
                f"  {cl._warn_label(sys.stdout)} gauge windows differ from the radar period by "
                f"up to {v['window_mismatch_s'] / 60:.1f} min (use --start/--end on the hour "
                "and --hold-edges-s)"
            )
    if res.ensemble is not None:
        _print_ensemble(res.ensemble, cl)
    for n in res.notes:
        print(f"  note: {n}")


def _rp(e: dict[str, Any]) -> str:
    rp = e["return_period_years"]
    if rp is None:
        return "-"
    flag = e.get("flag") or ""
    return (
        "<" if flag.startswith("below") else ">" if flag.startswith("above") else ""
    ) + f"{rp:g}"


def _m(m: dict[str, Any]) -> tuple[str, ...]:
    def f(x: Any, spec: str) -> str:
        return "-" if x is None else format(x, spec)

    rb = m.get("relative_bias")
    return (
        str(m.get("n", 0)),
        "-" if rb is None else f"{100 * rb:+.1f} %",
        f(m.get("rmse_mm"), ".2f"),
        f(m.get("correlation"), ".2f"),
        f(m.get("kge"), ".2f"),
    )


def _print_ensemble(ens: Any, cl: Any) -> None:
    em = ens.model
    print(
        cl.bold("ensemble")
        + f"  |  {ens.n_members} members, seed {ens.seed}  |  RETRIEVED, ASSUMED error model: "
        "not a forecast, not calibrated unless verified"
    )
    print(
        f"  error    sigma(ln) {em.sigma_ln:.3f}, correlation {em.corr_length_km:.0f} km, shares "
        f"common/spatial/cell {em.shares[0]:.2f}/{em.shares[1]:.2f}/{em.shares[2]:.2f}"
    )
    for k, v in em.sources.items():
        print(f"           {k}: {v}")
    for n in em.notes:
        print(f"  note: {n}")
    nan = float("nan")
    for r in ens.basins:
        bm = r.get("basin_mean_mm")
        if not bm:
            continue
        probs = ", ".join(
            f"P(>= {t} mm) {100 * p:.0f} %" for t, p in r["prob_basin_mean_ge"].items()
        )
        print(
            f"  {r['id']}: basin mean {bm['mean']:.1f} mm "
            f"(5-95 %: {bm.get('p05', nan):.1f} to {bm.get('p95', nan):.1f}); {probs}"
        )
    rel = ens.reliability
    if rel is None:
        print("  reliability  no gauges: the probabilities are NOT validated against observations")
    elif not rel.get("validated"):
        print(f"  reliability  {rel['note']}")
    else:
        pool = rel["pooled"]
        slope = pool["reliability_slope"] if pool["reliability_slope"] is not None else nan
        print(
            f"  reliability  {rel['n_gauges']} gauges: "
            f"Brier skill {pool['brier_skill_score']:.2f}, "
            f"slope {slope:.2f}"
            + (
                " (model fitted to the same gauges: optimistic)"
                if rel["model_from_the_same_gauges"]
                else ""
            )
        )

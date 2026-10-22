"""The ``rxd nowcast`` command (registered into :mod:`engine.cli`).

Light to import (argparse only); the engine is imported by the handler.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

EXAMPLES = (
    "examples:\n"
    "  rxd nowcast -i 'storm/KTLX/*_V06' --lead 60 --step 5 -o nowcast/\n"
    "  rxd nowcast -i 'storm/KTLX/*_V06' --ensemble 20 -o nowcast.nc -p nowcast.png\n"
    "  rxd nowcast -i frames_*.nc --lead 90 --origin 2021-12-11T02:00 -o out/\n"
    "  rxd nowcast -i 'storm/KTLX/*_V06' --verify -o scores.json\n"
    "\n"
    "Extrapolates the latest gridded frame along the motion of the last frames (Lucas-Kanade\n"
    "optical flow, semi-Lagrangian advection, Lagrangian persistence: no growth, decay or\n"
    "initiation). --ensemble N perturbs the motion with the verified motion error of past\n"
    "forecasts and gives probabilities. --verify scores every possible origin of the input\n"
    "sequence against the frames observed later, next to Eulerian persistence. Everything is\n"
    "labelled FORECAST: experimental guidance, not an official forecast (docs: Nowcast)."
)


def register(command: Any, cli: Any) -> None:
    p = command(
        "nowcast",
        "Advection nowcast of gridded radar frames, with ensemble, probabilities and verification.",
        EXAMPLES,
    )
    cli._add_input(p, "radar volumes in time (one or several radars) or gridded .nc frames")
    cli._add_output(p, "folder (nc + json + png) or files .nc / .json / .png (repeatable)")
    cli._add_plot(p, "also write the overview figure (.png/.pdf/.svg)")
    g = p.add_argument_group("forecast")
    g.add_argument(
        "--lead",
        type=float,
        default=60.0,
        metavar="MIN",
        help="longest lead time in minutes (default 60, at most 180)",
    )
    g.add_argument(
        "--step", type=float, default=5.0, metavar="MIN", help="lead step in minutes (default 5)"
    )
    g.add_argument(
        "--ensemble",
        type=int,
        default=0,
        metavar="N",
        help="members with perturbed motion (0 = deterministic only)",
    )
    g.add_argument(
        "--origin",
        metavar="TIME",
        help="forecast origin: the frame nearest this UTC time (default: the last frame)",
    )
    g.add_argument(
        "--motion",
        default="lucas_kanade",
        metavar="M",
        help="lucas_kanade (default) | global | none",
    )
    g.add_argument(
        "--n-frames",
        type=int,
        default=3,
        metavar="N",
        help="frames used for the motion (default 3)",
    )
    g.add_argument(
        "--decay",
        default="persistence",
        metavar="D",
        help="persistence (default) | exponential:<seconds>",
    )
    g.add_argument(
        "--relation",
        default="wsr88d",
        metavar="R",
        help="Z-R relation for the rain rate (rxd qpe --list-relations)",
    )
    g.add_argument(
        "--thresholds",
        metavar="T,T",
        help="thresholds of the probabilities in the input units (20,35,45 dBZ)",
    )
    g.add_argument(
        "--rate-thresholds", metavar="T,T", help="rain-rate thresholds in mm/h (0.5,2,10)"
    )
    g.add_argument(
        "--motion-stats",
        metavar="FILE",
        help="motion-error statistics JSON for the ensemble (default: the shipped verified table)",
    )
    g.add_argument("--seed", type=int, default=0, help="random seed of the ensemble (default 0)")
    g.add_argument(
        "--no-members", action="store_true", help="do not write the member fields to the NetCDF"
    )
    h = p.add_argument_group("gridding of radar volumes")
    h.add_argument("--resolution-m", type=float, default=1000.0, help="grid cell size (1000)")
    h.add_argument("--max-range-km", type=float, default=230.0, help="range limit per radar (230)")
    h.add_argument(
        "--extent", metavar="W,S,E,N", help="map box in degrees (default: around the radars)"
    )
    h.add_argument(
        "--product", default="composite", metavar="P", help="composite (default) | cappi"
    )
    h.add_argument(
        "-V",
        "--variable",
        default=None,
        metavar="NAME",
        help="variable (default DBZH; a .nc variable name for frames)",
    )
    h.add_argument(
        "--group-s",
        type=float,
        default=150.0,
        help="volumes within this many seconds form one frame (150)",
    )
    v = p.add_argument_group("verification")
    v.add_argument(
        "--verify",
        action="store_true",
        help="score every origin of the sequence against the later frames",
    )
    v.add_argument(
        "--origin-stride", type=int, default=1, help="use every n-th origin in --verify (1)"
    )
    v.add_argument(
        "--bootstrap",
        type=int,
        default=0,
        help="bootstrap resamples for the --verify intervals (0: one event, none)",
    )
    p.add_argument("--dpi", type=int, default=150, help="figure resolution (default 150)")
    p.set_defaults(func=cmd_nowcast)


def cmd_nowcast(args: argparse.Namespace, out: Any) -> int:
    import sys

    cl = sys.modules["engine.cli"]
    from engine.nowcast import frames as nf
    from engine.nowcast import run as nr

    files = cl._inputs(args)
    for target in args.output:
        ext = Path(target).suffix.lower()
        if ext and ext not in (".nc", ".json", ".png", ".pdf", ".svg", ".csv"):
            raise cl.UsageError(f"-o {target!r}: use a folder or .nc / .json / .png / .pdf / .svg")
    thr = (
        cl._numbers(args.thresholds, len(args.thresholds.split(",")), "--thresholds")
        if args.thresholds
        else None
    )
    rthr = (
        cl._numbers(args.rate_thresholds, len(args.rate_thresholds.split(",")), "--rate-thresholds")
        if args.rate_thresholds
        else (0.5, 2.0, 10.0)
    )
    extent = cl._numbers(args.extent, 4, "--extent") if args.extent else None
    try:
        seq = nf.load(
            files,
            variable=args.variable,
            progress=out.progress,
            resolution_m=args.resolution_m,
            max_range_km=args.max_range_km,
            extent=extent,
            product=args.product,
            group_s=args.group_s,
        )
    except nf.FrameError as exc:
        raise cl.UsageError(str(exc)) from exc
    out.progress_done()
    out.detail(
        f"{seq.n} frames, {seq.shape[1]} x {seq.shape[0]} cells @ {seq.cell_m:g} m, "
        f"step {seq.step_s() / 60:.1f} min, field {seq.field}"
    )
    if args.verify:
        return _verify(args, out, cl, seq, thr, rthr)
    origin = seq.index_at(args.origin) if args.origin else None
    try:
        nc = nr.nowcast(
            seq,
            lead_min=args.lead,
            step_min=args.step,
            n_members=args.ensemble,
            motion_method=args.motion,
            n_frames=args.n_frames,
            origin_index=origin,
            thresholds=thr,
            rate_thresholds=rthr,
            zr_relation=args.relation,
            decay=args.decay,
            motion_stats=args.motion_stats,
            seed=args.seed,
            keep_members=not args.no_members,
            progress=out.progress,
        )
    except (nr.NowcastError, ValueError) as exc:
        raise cl.UsageError(str(exc)) from exc
    out.progress_done()
    if not out.json:
        _print(nc, out, cl)
    outputs, plot = list(args.output), args.plot
    tag = cl._time_tag(_iso(nc.origin_time))
    if not outputs and not plot:
        outputs = [f"nowcast_{tag}"]
        out.chose(
            f"the folder nowcast_{tag}/ for the outputs (add -o / -p to choose)", "-o", outputs[0]
        )
    for target in outputs:
        out.wrote(*nc.save(target))
    if plot:
        from engine.api_results import save_figure

        out.wrote(save_figure(nc.plot(), plot, args.dpi))
    if out.json:
        from engine.api_results import jsonable

        payload = jsonable(nc.record())
        payload["files"] = out.files
        out.emit_json(payload)
    return cl.EXIT_OK


def _iso(t: Any) -> str:
    from engine import provenance

    return provenance.iso_utc(t)


def _print(nc: Any, out: Any, cl: Any) -> None:
    if out.quiet:
        return
    spd, drc = nc.motion.speed_direction()
    print(
        cl.bold("nowcast") + f"  |  origin {_iso(nc.origin_time)}  |  +{nc.leads_min[0]:g} to "
        f"+{nc.leads_min[-1]:g} min every {nc.config['step_min']:g} min"
        + f"  |  {nc.frames.shape[1]} x {nc.frames.shape[0]} cells @ {nc.frames.cell_m:g} m"
        f"  |  {nc.timing.get('total_s', 0):.1f} s"
    )
    print("  label    FORECAST: experimental guidance, not an official forecast")
    print(
        f"  motion   {nc.motion.method}: {spd:.1f} m/s towards {drc:.0f} deg "
        f"(reliable in {100 * nc.motion.reliable_fraction:.0f} % of cells)"
    )
    print(
        f"  members  {nc.n_members or 'none (deterministic)'}; motion error table: "
        f"{nc.motion_stats.source[:70]}"
    )
    rows = [
        (
            f"+{s['lead_min']:g}",
            s["valid_time"][11:16],
            f"{100 * s['forecast_fraction']:.0f} %",
            "-" if s["echo_cells"] is None else str(s["echo_cells"]),
            "-" if s["max"] is None else f"{s['max']:.1f}",
            f"{s['position_sigma_km']:.1f}",
        )
        for s in nc.stats()["leads"]
    ]
    print(
        cl._table(
            (
                "lead min",
                "valid UTC",
                "forecastable",
                f"cells >= {nc.thresholds[0]:g}",
                f"max {nc.units}",
                "pos. sigma km",
            ),
            rows,
            "  ",
        )
    )
    for n in nc.notes:
        print(f"  note: {n}")


def _verify(args: argparse.Namespace, out: Any, cl: Any, seq: Any, thr: Any, rthr: Any) -> int:
    import json

    from engine.api_results import jsonable
    from engine.nowcast import harness

    records = harness.verify_sequence(
        seq,
        event=Path(seq.sources[0]).name[:4] if seq.sources else "sequence",
        lead_min=args.lead,
        step_min=args.step,
        n_members=args.ensemble,
        n_frames=args.n_frames,
        thresholds=thr,
        rate_thresholds=rthr,
        zr_relation=args.relation,
        origin_stride=args.origin_stride,
        seed=args.seed,
        progress=out.progress,
    )
    out.progress_done()
    summary = harness.summarise(records, n_boot=args.bootstrap)
    if not out.json and not out.quiet:
        _print_verification(summary, out, cl)
    outputs = list(args.output) or [f"nowcast_verification_{cl._time_tag(None)}.json"]
    for target in outputs:
        p = Path(target)
        if p.suffix.lower() == ".json" or p.suffix == "":
            p = p if p.suffix else p / "nowcast_verification.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(
                    jsonable(
                        {
                            "summary": summary,
                            "frames": seq.describe(),
                            "options": {k: v for k, v in vars(args).items() if k not in ("func",)},
                        }
                    ),
                    indent=2,
                )
                + "\n"
            )
            out.wrote(p)
        else:
            raise cl.UsageError("--verify writes a .json summary (or a folder)")
    if out.json:
        out.emit_json(jsonable({"summary": summary, "files": out.files}))
    return cl.EXIT_OK


def _print_verification(summary: dict[str, Any], out: Any, cl: Any) -> None:
    thr = summary["thresholds"]
    leads = summary["leads_min"]
    print(
        cl.bold("verification") + f"  |  {summary['n_records']} forecast-lead records  |  events: "
        f"{', '.join(summary['events'])}"
    )
    print(
        "  CSI at the lowest threshold and MAE of the rain rate (mm/h), per model and lead; "
        "skill = model - Eulerian persistence"
    )
    t0 = thr[0] if thr else None
    rows = []
    for ld in leads:
        row = [f"+{ld:g}"]
        for m in summary["models"]:
            s = summary["by_model_lead"].get(m, {}).get(ld)
            if not s:
                row += ["-", "-"]
                continue
            csi = s["categorical"].get(t0, {}).get("csi") if t0 is not None else None
            mae = s["continuous"].get("mae")
            row += ["-" if csi is None else f"{csi:.3f}", "-" if mae is None else f"{mae:.3f}"]
        rows.append(tuple(row))
    heads = ["lead"]
    for m in summary["models"]:
        heads += [f"CSI {m[:6]}", f"MAE {m[:6]}"]
    print(cl._table(tuple(heads), rows, "  "))

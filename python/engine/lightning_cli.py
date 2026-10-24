"""``rxd lightning``: read GOES GLM or LMA files, cluster flashes, count them per minute.

Registered by :func:`engine.cli.build_parser`; the work is in :mod:`engine.lightning_layer` (the same
functions the viewer's lightning layer uses). Files are read from disk; nothing is downloaded.
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import csv
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np

EPILOG = (
    "examples:\n"
    "  rxd lightning -i 'glm/OR_GLM-L2-LCFA_G16_s2021345024*.nc'\n"
    "  rxd lightning -i glm/*.nc --box 36 -92 39 -86 --bin-s 60 -o flashes.csv rates.csv\n"
    "  rxd lightning -i LYLOUT_211211_020000_0600.dat --link-distance 6000 --link-time 0.15 --json\n"
    "  rxd lightning -i glm/*.nc --parallax-km 10      # move GLM flashes to a 10 km cloud top (an assumption)\n"
    "\n"
    "GLM: flashes and groups as the Level 2 product grouped them. LMA: sources are linked into flashes\n"
    "by the space-time metric of Fuchs et al. (2016) (defaults 3 km, 0.15 s, the values they used for the\n"
    "Colorado network). -o accepts .csv (one row per flash for flashes.csv, or per bin when the name contains\n"
    "'rate') and .json. Times are UTC. The data are OBSERVED; detection efficiency is not computed."
)


def add_parser(
    command: Callable[..., argparse.ArgumentParser],
    add_input: Callable[..., None],
    add_output: Callable[..., None],
) -> argparse.ArgumentParser:
    p = command(
        "lightning",
        "Observed lightning (GOES GLM, LMA): flashes, flash clustering and flash rate per minute.",
        EPILOG,
    )
    add_input(p, "GLM Level 2 .nc files or LMA .dat files")
    add_output(
        p, "write flashes (.csv), the rate (.csv with rate in its name) or everything (.json)"
    )
    p.add_argument(
        "--box",
        nargs=4,
        type=float,
        metavar=("SOUTH", "WEST", "NORTH", "EAST"),
        help="keep flashes inside this box (degrees)",
    )
    p.add_argument("--from", dest="t0", metavar="TIME", help="first time to keep (ISO, UTC)")
    p.add_argument("--to", dest="t1", metavar="TIME", help="last time to keep (ISO, UTC)")
    p.add_argument(
        "--bin-s", type=float, default=60.0, metavar="S", help="bin length of the rate (default 60)"
    )
    p.add_argument(
        "--parallax-km",
        type=float,
        metavar="KM",
        help="GLM: move flashes to this cloud-top height (an assumption; see the docs)",
    )
    p.add_argument(
        "--all-quality", action="store_true", help="GLM: keep flashes with a quality flag"
    )
    p.add_argument("--max-chi2", type=float, default=1.0, help="LMA: chi-square limit (default 1)")
    p.add_argument("--min-stations", type=int, default=6, help="LMA: stations needed (default 6)")
    p.add_argument(
        "--link-distance",
        type=float,
        default=3000.0,
        metavar="M",
        help="LMA: D in metres (default 3000)",
    )
    p.add_argument(
        "--link-time",
        type=float,
        default=0.15,
        metavar="S",
        help="LMA: T in seconds (default 0.15)",
    )
    p.add_argument(
        "--min-points", type=int, default=1, help="LMA: sources needed to start a flash (default 1)"
    )
    p.add_argument(
        "--max-duration",
        type=float,
        default=3.0,
        metavar="S",
        help="LMA: longest flash in s (default 3; 0 = no limit)",
    )
    p.set_defaults(func=cmd_lightning)
    return p


def cmd_lightning(args: argparse.Namespace, out: Any) -> int:
    from engine import cli
    from engine import lightning_layer as ll
    from engine.api_results import XrdDependencyError, XrdInputError

    files = cli._inputs(args)
    kw: dict[str, Any] = {}
    if args.box:
        kw["box"] = tuple(args.box)
    if args.t0:
        kw["t0"] = args.t0
    if args.t1:
        kw["t1"] = args.t1
    nc = any(Path(f).suffix.lower() in (".nc", ".nc4") for f in files)
    if nc:
        if args.parallax_km is not None:
            kw["parallax_to_km"] = args.parallax_km
        kw["good_only"] = not args.all_quality
    else:
        kw["max_chi2"] = args.max_chi2
        kw["min_stations"] = args.min_stations
        kw["link"] = {
            "distance_m": args.link_distance,
            "time_s": args.link_time,
            "min_points": args.min_points,
            "max_duration_s": args.max_duration or None,
        }
    try:
        ls = ll.read_lightning(files, **kw)
    except (ValueError, FileNotFoundError) as exc:
        raise XrdInputError(str(exc)) from exc
    except ImportError as exc:
        raise XrdDependencyError(str(exc)) from exc

    cov = ls.coverage
    rate = None
    if cov:
        start, end = cov[0][0], cov[-1][1]
        if end > start:
            rate = ll.rate_series(ls.f_t0, start, end, args.bin_s, cov)
    t0s = [
        (ls.t_ref + timedelta(seconds=float(t))).isoformat().replace("+00:00", "Z") for t in ls.f_t0
    ]

    for target in args.output:
        suffix = Path(target).suffix.lower()
        if suffix == ".json":
            Path(target).write_text(cli.dumps(ll.to_wire(ls)) + "\n", encoding="utf-8")
        elif suffix == ".csv":
            with open(target, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                fh.write(
                    f"# {ls.instrument}; class OBSERVED; times UTC; files: {', '.join(ls.files[:5])}\n"
                )
                if "rate" in Path(target).name.lower() and rate is not None:
                    w.writerow(["bin_start_utc", "flashes", "flashes_per_minute"])
                    for k in range(len(rate["count"])):
                        start_k = (
                            (ls.t_ref + timedelta(seconds=float(rate["edges"][k])))
                            .isoformat()
                            .replace("+00:00", "Z")
                        )
                        c = rate["count"][k]
                        w.writerow(
                            [
                                start_k,
                                "" if np.isnan(c) else int(c),
                                "" if np.isnan(c) else f"{rate['rate_per_min'][k]:.4g}",
                            ]
                        )
                else:
                    w.writerow(
                        [
                            "start_utc",
                            "latitude",
                            "longitude",
                            "height_km_asl",
                            "n_points",
                            "energy_j",
                            "area_km2",
                        ]
                    )
                    for k in range(ls.n_flashes):
                        alt = ls.f_alt_km[k]
                        w.writerow(
                            [
                                t0s[k],
                                f"{ls.f_lat[k]:.5f}",
                                f"{ls.f_lon[k]:.5f}",
                                "" if np.isnan(alt) else f"{alt:.3f}",
                                int(ls.f_n_points[k]),
                                "" if np.isnan(ls.f_strength[k]) else f"{ls.f_strength[k]:.4g}",
                                "" if np.isnan(ls.f_area_km2[k]) else f"{ls.f_area_km2[k]:.4g}",
                            ]
                        )
        else:
            raise cli.UsageError(
                f"-o for lightning must end in .csv or .json, not {suffix or target!r}"
            )
        out.wrote(target)

    if out.json:
        wire = ll.to_wire(ls)
        wire.pop("points", None)
        wire.pop("flashes", None)
        wire["n_flashes"] = ls.n_flashes
        wire["n_points"] = ls.n_points
        if rate is not None:
            wire["rate"] = {
                "bin_s": args.bin_s,
                "count": [None if np.isnan(c) else int(c) for c in rate["count"]],
            }
        out.emit_json(wire)
        return cli.EXIT_OK
    out.result(f"{ls.instrument}  ({len(ls.files)} files)  class: OBSERVED")
    out.result(
        f"{ls.n_flashes} flashes, {ls.n_points} {'groups' if ls.kind == 'glm' else 'sources'}"
    )
    if ls.n_flashes:
        out.result(f"first flash {t0s[0]}, last flash {t0s[-1]} (UTC)")
    if rate is not None and ls.n_flashes:
        good = rate["rate_per_min"][~np.isnan(rate["rate_per_min"])]
        if good.size:
            out.result(
                f"flash rate per {args.bin_s:g} s bin: mean {good.mean():.3g} per minute, peak {good.max():.3g} per minute; bins without data: {int(np.isnan(rate['rate_per_min']).sum())}"
            )
    par = ls.meta.get("parallax")
    if par:
        out.result(
            f"parallax: {par['uncertainty_shift_km']:.2g} km for {par['uncertainty_cloud_top_error_km']:g} km of cloud-top height error; "
            + (
                f"corrected to {par['applied_to_km']:g} km"
                if par["applied_to_km"] is not None
                else "not corrected"
            )
            + f". {par['assumption']}"
        )
    if ls.kind == "lma":
        out.result(f"clustering: {ls.meta['link']}; quality filter {ls.meta['qc']}")
    for note in ls.notes:
        out.result(f"note: {note}")
    return cli.EXIT_OK

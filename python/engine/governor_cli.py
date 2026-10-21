"""``rxd budget``: what this machine allows and what each operation would cost on the inputs.

Registered by :func:`engine.cli.build_parser`; the work is in :mod:`engine.governor`.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from typing import Any

EPILOG = (
    "examples:\n"
    "  rxd budget -i KHPX20211211_015357_V06\n"
    "  rxd budget -i 'mayfield/*/*_V06' --json\n"
    "  rxd budget -i data/ --budget-gb 4 --ops mosaic winds\n"
    "  rxd budget -i data/ --max-minutes 10      # also thin long time series to fit 10 minutes\n"
    "\n"
    "Nothing is processed: one file per radar is opened to measure it, the others are scaled by\n"
    "their size. For each operation the estimate (peak memory, time) is shown with what would be\n"
    "done: run as asked, run with stated changes (each one says what is lost), or not run (with\n"
    "what you can change). The same decision is taken automatically when the command runs.\n"
    "--budget-gb (or RADARX_BUDGET_GB) sets the memory budget for every rxd command."
)


def add_parser(
    command: Callable[..., argparse.ArgumentParser], add_input: Callable[..., None]
) -> argparse.ArgumentParser:
    p = command(
        "budget",
        "How much this computer allows, and what each operation would cost on these files.",
        EPILOG,
    )
    add_input(p, "radar files, globs or folders to plan for")
    p.add_argument(
        "--ops",
        nargs="+",
        metavar="OP",
        choices=["read", "qc", "mosaic", "winds", "cells", "animate", "export"],
        help="operations to plan (default: all of read qc mosaic winds cells animate export)",
    )
    p.add_argument(
        "--out-dir", metavar="DIR", help="folder whose free disk space counts (default: here)"
    )
    p.add_argument(
        "--max-minutes", type=float, metavar="MIN", help="time limit: thin long time series"
    )
    p.add_argument(
        "--probe",
        type=int,
        default=1,
        metavar="N",
        help="files opened per radar to measure (default 1)",
    )
    p.set_defaults(func=cmd_budget)
    return p


def cmd_budget(args: argparse.Namespace, out: Any) -> int:
    from engine import cli, governor

    files = cli._inputs(args)
    rep = governor.report(
        files,
        budget_gb=getattr(args, "budget_gb", None),
        out_dir=args.out_dir,
        operations=args.ops or governor.OPERATIONS,
        probe=max(1, args.probe),
        max_seconds=args.max_minutes * 60 if args.max_minutes else None,
    )
    if out.json:
        out.emit_json(governor.report_dict(rep))
    else:
        out.result(governor.report_text(rep))
    return cli.EXIT_OK


def apply_budget_option(args: argparse.Namespace) -> None:
    """``--budget-gb`` of any command becomes the budget of this process (and its children)."""
    gb = getattr(args, "budget_gb", None)
    if gb is not None:
        if not gb > 0:
            raise ValueError("--budget-gb must be a positive number of gigabytes")
        os.environ["RADARX_BUDGET_GB"] = str(gb)

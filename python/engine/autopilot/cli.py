"""``rxd auto INPUT... [--plan-only] [--yes] [--budget-gb N] [--out DIR] [--steps a,b] [--json]``.

Registered by ``engine.cli.build_parser`` through :func:`add_parser`; importing this module is
light (argparse only).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

EPILOG = (
    "examples:\n"
    "  rxd auto /data/mayfield --plan-only         inspect and show the plan, run nothing\n"
    "  rxd auto KHPX*_V06 KPAH*_V06 --yes -o out   run every enabled step without asking\n"
    "  rxd auto folder/ --steps check,cells -o out run only these steps\n"
    "  rxd auto folder/ --budget-gb 4 --json       plan under a 4 GB memory budget, as JSON\n"
    "\n"
    "The plan lists every step with why, what it needs, an estimate for THIS data, what it\n"
    "produces, its provenance class and the equivalent rxd command; refusals say what is\n"
    "missing. Nothing runs or is written before you approve (--yes, --steps, or the prompt)."
)


def add_parser(
    command: Callable[..., argparse.ArgumentParser],
    add_input: Callable[..., None],
    add_output: Callable[..., None],
) -> argparse.ArgumentParser:
    p = command(
        "auto",
        "Autopilot: inspect any set of inputs, plan the analyses that make sense, "
        "run the approved ones.",
        EPILOG,
    )
    add_input(p, "radar files, folders or globs: one file, a series, several radars, mixed")
    add_output(p, "folder for the results, run record and summary (default: ./autopilot_<time>)")
    p.add_argument("--plan-only", action="store_true", help="inspect and plan, run nothing")
    p.add_argument("--yes", "-y", action="store_true", help="run every enabled step without asking")
    p.add_argument(
        "--steps", metavar="IDS", help="comma-separated step ids to run (implies consent for those)"
    )
    # --budget-gb is a global option of every rxd command (engine/governor_cli.py): not repeated here.
    p.add_argument(
        "--no-check", action="store_true", help="skip the data check during inspection (faster)"
    )
    p.set_defaults(func=cmd_auto)
    return p


def cmd_auto(args: argparse.Namespace, out: Any) -> int:
    from datetime import datetime

    from engine import cli
    from engine.autopilot import inspect_inputs, make_plan, run_plan
    from engine.autopilot.inspect import AutopilotError
    from engine.autopilot.plan import format_plan

    files = cli._inputs(args)
    quiet = out.json or out.quiet

    def progress(pct: int, text: str) -> None:
        if not quiet:
            print(f"\r  {pct:3d}% {text[:70]:<70}", end="", file=sys.stderr, flush=True)

    try:
        inv = inspect_inputs(files, progress=progress, data_check=not args.no_check)
        if not quiet:
            print("", file=sys.stderr)
        out_dir = (
            args.output[0]
            if args.output
            else f"autopilot_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
        )
        plan = make_plan(inv, budget_gb=args.budget_gb, out_dir=out_dir)
    except AutopilotError as exc:
        raise cli.UsageError(str(exc)) from exc
    if args.plan_only or not (args.yes or args.steps):
        if out.json:
            out.emit_json({"inventory": inv, "plan": plan})
        else:
            out.result(format_plan(plan, inv))
            if not args.plan_only:
                out.note(
                    "Nothing was run. Add --yes to run the enabled steps, "
                    "or --steps IDS, with -o DIR."
                )
        return cli.EXIT_OK
    step_ids = [s.strip() for s in args.steps.split(",") if s.strip()] if args.steps else None
    try:
        record = run_plan(inv, plan, out_dir=out_dir, step_ids=step_ids, progress=progress)
    except AutopilotError as exc:
        raise cli.UsageError(str(exc)) from exc
    if not quiet:
        print("", file=sys.stderr)
    if out.json:
        out.emit_json(record)
    else:
        for s in record["steps"]:
            out.result(
                f"{s['status']:>10}  {s['title']}" + (f": {s['error']}" if s.get("error") else "")
            )
        out.wrote(record["summary_path"], record["record_path"])
        out.result(Path(record["summary_path"]).read_text())
    failed = any(s["status"] == "failed" for s in record["steps"])
    return cli.EXIT_FAIL if failed else cli.EXIT_OK

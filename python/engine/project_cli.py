"""``rxd methods`` and ``rxd project``: the methods text of a run record, and project files.

Registered by :func:`engine.cli.build_parser`; the work is in :mod:`engine.runrecord` and
:mod:`engine.project`. Standard library only at import, like the rest of the command line.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

METHODS_EPILOG = (
    "examples:\n"
    "  rxd methods run_record.json                 # the methods text, one paragraph per step\n"
    "  rxd methods storm.radarxproj -o methods.txt # a project file carries its run record\n"
    "\n"
    "The record is saved from the app (Export panel, 'Run record') or is part of a project file.\n"
    "The text names every parameter of every step, the software and library versions\n"
    "(radarx and its version for every step radarx computes) and the references."
)

PROJECT_EPILOG = (
    "examples:\n"
    "  rxd project show storm.radarxproj    # files, recipe, run record, where the files are\n"
    "  rxd project check storm.radarxproj   # exit code 3 when a file is missing or changed\n"
    "\n"
    "A project holds no data: it records the files (path and path relative to the project, size\n"
    "and modification time), what was on screen, the pipeline recipe and the run record. The app\n"
    "opens one with File > Open project."
)


def add_parser(command: Callable[..., argparse.ArgumentParser]) -> None:
    p = command(
        "methods",
        "Print the methods text (every step, parameter and version) of a run record.",
        METHODS_EPILOG,
    )
    p.add_argument("record", metavar="FILE", help="a run record (.json) or a project file")
    p.add_argument("-o", "--output", metavar="FILE", help="write the text here instead of printing")
    p.set_defaults(func=cmd_methods)

    p = command(
        "project",
        "Show or check a project file (the files, recipe and run record of a piece of work).",
        PROJECT_EPILOG,
    )
    p.add_argument("action", choices=["show", "check"], help="show it, or check its files")
    p.add_argument("file", metavar="FILE", help="a project file (.radarxproj)")
    p.set_defaults(func=cmd_project)


def _read_record(path: str) -> dict[str, Any]:
    from engine import project, runrecord

    text = Path(path).expanduser()
    try:
        head = text.read_text(encoding="utf-8")[:200]
    except OSError as exc:
        raise FileNotFoundError(f"{text.name}: {exc.strerror}") from exc
    if '"radarx-project/' in head or text.suffix == project.EXTENSION:
        doc = project.read_project(text)
        if not doc.get("run_record"):
            raise runrecord.RunRecordError(
                f"{text.name} holds no run record (nothing was processed before it was saved)."
            )
        return runrecord.validate(doc["run_record"])
    return runrecord.load_record(text)


def cmd_methods(args: argparse.Namespace, out: Any) -> int:
    from engine import cli, project, runrecord

    try:
        record = _read_record(args.record)
    except FileNotFoundError as exc:
        print(f"{cli._err_label(sys.stderr)} {exc}", file=sys.stderr)
        return cli.EXIT_INPUT
    except (runrecord.RunRecordError, project.ProjectError) as exc:
        raise cli.UsageError(str(exc)) from None
    text = runrecord.methods_text(record)
    if out.json:
        out.emit_json({"paragraphs": runrecord.methods_paragraphs(record), "record": record})
    elif args.output:
        Path(args.output).expanduser().write_text(text, encoding="utf-8")
        out.note(f"wrote {args.output}")
    else:
        sys.stdout.write(text)
    return cli.EXIT_OK


def cmd_project(args: argparse.Namespace, out: Any) -> int:
    from engine import cli, project

    try:
        loaded = project.load_project(args.file)
    except project.ProjectError as exc:
        if "not found" in str(exc):
            print(f"{cli._err_label(sys.stderr)} {exc}", file=sys.stderr)
            return cli.EXIT_INPUT
        raise cli.UsageError(str(exc)) from None
    problems = [r for r in loaded["resolved"] if r["status"] != "found"]
    if out.json:
        out.emit_json(loaded)
    else:
        out.result(project.summary(loaded))
        if args.action == "check":
            out.result(
                "All files are where the project saved them."
                if not problems
                else f"{len(problems)} of {len(loaded['resolved'])} files are missing or changed."
            )
    return cli.EXIT_INPUT if args.action == "check" and problems else cli.EXIT_OK

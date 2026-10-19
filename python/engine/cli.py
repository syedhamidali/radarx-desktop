"""``xrd``: the command line of xradar-desktop.

Everything the desktop app does is available here without the GUI, on top of the same engine
(:mod:`engine.api`). The commands follow the order a scientist works in::

    Look      xrd info -i FILE            what is in a radar file
              xrd plot -i FILE            a PPI picture of one variable
    Clean     xrd pipeline RECIPE -i FILE QC recipes (list | show | run | script)
    Analyse   xrd run MODULE -i FILE      an algorithm, one sweep or all sweeps
              xrd modules                 list | show the algorithms
              xrd cells -i FILE [FILE..]  storm cells / tracks
    Combine   xrd mosaic -i F1 F2 ...     several radars on one map
              xrd winds -i F1 F2 ...      multi-Doppler winds
    Save      xrd export -i FILE -o OUT   NetCDF / Zarr / CSV / image
    Help      xrd examples                a guided tour of copy-paste commands
              xrd completion bash|zsh|fish

and every command uses the same options::

    -i / --input FILE [FILE ...]   input file(s), globs, folders or ``-`` (a list on stdin)
    -o / --output PATH             the result file(s); the extension says what is written
    -p / --plot PATH               also write a figure (.png / .svg / .pdf) of the result
    -P / --param KEY=VALUE         module / pipeline / mosaic / winds parameter (repeatable)
    -V / --variable NAME           variable (any name the variable mapping knows)
    -s / --sweep N                 sweep index (default: the lowest sweep that has what is needed)
    -f / --format FMT              when the -o extension is not enough
    -v / --verbose, -q / --quiet, --json, --config FILE, --print-command

Exit codes: 0 success; 1 the processing failed (module could not run on this data ...);
2 the command was used wrongly (unknown option, module, variable ...); 3 an input file is
missing or unreadable; 4 an optional Python package is missing (the message says how to install
it); 70 an unexpected internal error (re-run with ``-vv`` for the traceback); 130 interrupted.

This module imports only the standard library at start-up; the engine is imported by the
command that needs it, so ``xrd --help`` is instant and the sidecar server never loads it.
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import shlex
import sys
import time
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_DEPENDENCY = 4
EXIT_INTERNAL = 70
EXIT_INTERRUPTED = 130

PROG = "xrd"
SAMPLE = "KTLX20130520_201643_V06.gz"

#: Files with these suffixes are never taken for radar volumes when a folder is expanded.
NON_RADAR_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".pdf", ".svg", ".csv", ".json", ".txt", ".md", ".pgw", ".prj",
    ".toml", ".yaml", ".yml", ".py", ".ipynb", ".html", ".log", ".tif", ".tiff", ".zip",
}  # fmt: skip

COMMANDS = (
    "info",
    "plot",
    "pipeline",
    "run",
    "modules",
    "cells",
    "sounding",
    "mosaic",
    "winds",
    "export",
    "examples",
    "completion",
)


# ---------------------------------------------------------------------------
# Output helpers (colour, JSON, progress)
# ---------------------------------------------------------------------------


def _color_enabled(stream: Any) -> bool:
    """Colour only on a terminal, never when ``NO_COLOR`` is set (https://no-color.org)."""
    if os.environ.get("NO_COLOR") or _STATE["no_color"]:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _paint(text: str, code: str, stream: Any) -> str:
    return f"\033[{code}m{text}\033[0m" if _color_enabled(stream) else text


def bold(text: str, stream: Any = None) -> str:
    return _paint(text, "1", stream or sys.stdout)


def _err_label(stream: Any) -> str:
    return _paint("error:", "1;31", stream)


def _warn_label(stream: Any) -> str:
    return _paint("warning:", "33", stream)


def dumps(obj: Any) -> str:
    from engine.api_results import dumps as _dumps

    return _dumps(obj)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], indent: str = "  ") -> str:
    """Plain aligned text table (numbers right-aligned)."""
    cells = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    numeric = [
        bool(cells) and all(_is_number(r[i]) for r in cells if r[i] not in ("", "-"))
        for i in range(len(headers))
    ]

    def fmt(row: Sequence[str]) -> str:
        return (
            indent
            + "  ".join(
                (c.rjust(w) if numeric[i] else c.ljust(w))
                for i, (c, w) in enumerate(zip(row, widths, strict=True))
            ).rstrip()
        )

    lines = [fmt(list(headers)), indent + "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in cells]
    return "\n".join(lines)


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _human_bytes(n: float | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} TB"  # pragma: no cover


def _seconds(ms: float) -> str:
    return f"{ms / 1000.0:.1f} s" if ms >= 100 else f"{ms:.0f} ms"


def _show(path: Path | str) -> str:
    """A path as short as is unambiguous: relative to the current directory when inside it."""
    p = Path(path)
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve()))
    except (ValueError, OSError):
        return str(p)


class Out:
    """Where a command writes: results to stdout (text or one JSON document), chatter to stderr."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.json: bool = bool(getattr(args, "json", False))
        self.quiet: bool = bool(getattr(args, "quiet", False))
        self.verbose: int = int(getattr(args, "verbose", 0) or 0)
        self.files: list[str] = []
        self.auto: list[str] = []  # what was chosen for the user, one sentence each
        self.auto_args: list[str] = []  # the same choices as explicit command-line options
        self._last_pct = -1

    def result(self, text: str) -> None:
        """The command's main text output (suppressed only by ``--json``)."""
        if not self.json:
            print(text)

    def emit_json(self, obj: Any) -> None:
        print(dumps(obj))

    def note(self, text: str) -> None:
        if not self.quiet:
            print(text, file=sys.stderr)

    def chose(self, text: str, *args: str) -> None:
        """Say what was picked automatically (``-q`` and ``--json`` keep it out of the way)."""
        self.auto.append(text)
        self.auto_args.extend(args)
        if not self.json and not self.quiet:
            print(f"using {text}")

    def detail(self, text: str) -> None:
        """Extra output shown with ``-v``."""
        if self.verbose and not self.json and not self.quiet:
            print(text)

    def warn(self, text: str) -> None:
        print(f"{_warn_label(sys.stderr)} {text}", file=sys.stderr)

    def wrote(self, *paths: Path | str) -> None:
        """Record (and announce) the files that were written."""
        for path in paths:
            self.files.append(str(path))
            if not self.json and not self.quiet:
                print(f"wrote {_show(path)}")

    def progress(self, percent: int, message: str) -> None:
        """Progress callback for the engine (``-v``): one updating line on a terminal."""
        if not self.verbose or self.quiet or self.json:
            return
        if sys.stderr.isatty():
            sys.stderr.write(f"\r[{percent:3d}%] {message}"[:100].ljust(100) + "\r")
            sys.stderr.flush()
        elif percent // 10 != self._last_pct // 10 or percent >= 100:
            print(f"[{percent:3d}%] {message}", file=sys.stderr)
        self._last_pct = percent

    def progress_done(self) -> None:
        if self.verbose and not self.quiet and not self.json and sys.stderr.isatty():
            sys.stderr.write(" " * 100 + "\r")
            sys.stderr.flush()
        self._last_pct = -1


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class UsageError(Exception):
    """The command line was wrong (reported with exit code 2)."""


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that raises instead of exiting, so ``main(argv)`` can return a code."""

    def error(self, message: str) -> Any:  # type: ignore[override]
        match = re.search(r"invalid choice: '([^']+)' \(choose from (.*)\)", message)
        if match:
            choices = [c.strip(" '") for c in match.group(2).split(",")]
            close = difflib.get_close_matches(match.group(1), choices, n=2, cutoff=0.5)
            if close:
                message = (
                    f"unknown value '{match.group(1)}'. Did you mean "
                    f"{' or '.join(repr(c) for c in close)}?"
                )
        raise UsageError(f"{message}\n\nRun '{self.prog} --help' for usage and examples.")


class _Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=34, width=100)

    def _format_action_invocation(self, action: argparse.Action) -> str:
        # Python < 3.13 repeats the metavar after every flag ("-i FILE, --input FILE"); always print
        # the short form "-i, --input FILE" so the help reads the same on every supported Python.
        if not action.option_strings or action.nargs == 0:
            return super()._format_action_invocation(action)
        metavar = self._format_args(action, self._get_default_metavar_for_optional(action))
        return f"{', '.join(action.option_strings)} {metavar}"


def _common_options() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("output control")
    g.add_argument("--json", action="store_true", help="print machine-readable JSON on stdout")
    g.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="show progress and more detail (-vv: engine log and tracebacks)",
    )
    g.add_argument("-q", "--quiet", action="store_true", help="print only errors")
    g.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--no-color", action="store_true", help="never use colour (or set NO_COLOR=1)")
    h = p.add_argument_group("reproducibility")
    h.add_argument(
        "--config",
        metavar="FILE",
        help="read options from a .toml/.yaml/.json file (same names as the long options)",
    )
    h.add_argument(
        "--print-command",
        action="store_true",
        help="print the equivalent full command at the end (with everything chosen for you)",
    )
    return p


def _add_input(p: argparse.ArgumentParser, what: str = "radar file(s)") -> None:
    p.add_argument(
        "-i",
        "--input",
        nargs="+",
        action="extend",
        default=[],
        metavar="FILE",
        help=f"input {what}: files, globs, folders or - (list on stdin); repeatable; bare FILEs work too",
    )


def _add_output(p: argparse.ArgumentParser, desc: str) -> None:
    p.add_argument(
        "-o", "--output", "--out", action="append", default=[], metavar="PATH", help=desc
    )


def _add_plot(
    p: argparse.ArgumentParser, desc: str = "also write a figure (.png/.pdf/.svg)"
) -> None:
    p.add_argument("-p", "--plot", metavar="PATH", help=desc)


def _add_param(p: argparse.ArgumentParser, desc: str) -> None:
    p.add_argument("-P", "--param", action="append", default=[], metavar="KEY=VALUE", help=desc)


def _add_sweep(p: argparse.ArgumentParser, *, all_sweeps: bool = False) -> None:
    auto = "sweep index (default: the lowest sweep that has what is needed)"
    if all_sweeps:
        g = p.add_mutually_exclusive_group()
        g.add_argument("-s", "--sweep", type=int, metavar="N", help=auto)
        g.add_argument(
            "--all-sweeps",
            action="store_true",
            help="process every sweep; stores one derived variable per field",
        )
    else:
        p.add_argument("-s", "--sweep", type=int, metavar="N", help=auto)


def _add_variable(p: argparse.ArgumentParser, default: str | None, what: str = "variable") -> None:
    shown = f" (default {default})" if default else " (default: reflectivity)"
    p.add_argument(
        "-V",
        "--variable",
        default=default,
        metavar="NAME",
        help=f"{what}{shown}; velocity etc. work",
    )


def _add_format(p: argparse.ArgumentParser, desc: str) -> None:
    p.add_argument("-f", "--format", metavar="FMT", help=desc)


GROUPS = (
    ("Look", (("info", "what is in a file: format, radar, time, sweeps, variables"),
              ("plot", "draw a PPI picture of one variable"))),
    ("Clean", (("pipeline", "apply a QC recipe: clutter, dealiasing, speckle ... (list | show | run | script)"),)),
    ("Analyse", (("run", "run an algorithm: hydrometeor classes, shear, rain rate ... (= modules run)"),
                 ("modules", "list | show the algorithms and their parameters"),
                 ("cells", "find storm cells; several files are tracked in time"),
                 ("sounding", "read a radiosonde file: CAPE, CIN, shear, helicity, freezing level"))),
    ("Combine", (("mosaic", "merge several radars onto one map"),
                 ("winds", "3-D winds from two or more radars (multi-Doppler)"))),
    ("Save", (("export", "write NetCDF / CfRadial2 / Zarr / CSV / an image"),)),
    ("Help", (("examples", "a guided tour of copy-paste commands"),
              ("completion", "shell completion script for bash, zsh or fish"))),
)  # fmt: skip


def overview() -> str:
    """The short, friendly text printed by plain ``xrd``."""
    lines = [
        bold("xrd") + " - radar data from the command line (xradar-desktop, no GUI needed)",
        "",
    ]
    for title, items in GROUPS:
        for i, (name, text) in enumerate(items):
            lines.append(f"  {title if i == 0 else '':<8}{name:<12}{text}")
    lines += [
        "",
        "Try these (replace the file name with yours):",
        f"  xrd info -i {SAMPLE}                      what is in the file",
        f"  xrd plot -i {SAMPLE}                      reflectivity picture -> PNG",
        f"  xrd run radarx_hid -i {SAMPLE}            hydrometeor classes of the lowest suitable sweep",
        "",
        "Every command takes  -i INPUT  -o OUTPUT  -p FIGURE  (see 'xrd COMMAND --help').",
        "More: xrd examples      a tour of what you can do",
        "      xrd modules       the algorithms        xrd pipeline list   the QC recipes",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """The ``xrd`` argument parser (also used by the tests, the completion script and the docs)."""
    common = _common_options()
    listing = []
    for title, items in GROUPS:
        for i, (name, text) in enumerate(items):
            listing.append(f"  {title if i == 0 else '':<8}{name:<12}{text}")
    parser = _Parser(
        prog=PROG,
        usage="xrd [--version] COMMAND [options]",
        formatter_class=_Formatter,
        allow_abbrev=False,
        description=(
            "xradar-desktop on the command line: look at radar files, clean them, run the\n"
            "algorithms, combine radars and save the results, without the GUI.\n"
            "\n" + "\n".join(listing) + "\n\n"
            "The same options mean the same thing in every command:\n"
            "  -i, --input FILE [FILE ...]   input radar file(s), globs, folders, or - for a list on stdin\n"
            "  -o, --output PATH             result file; the extension (.nc .zarr .csv .json .png) says what\n"
            "                                is written; without -o a sensible name is chosen and printed\n"
            "  -p, --plot PATH               also write a figure (.png .pdf .svg) of the result\n"
            "  -P, --param KEY=VALUE         parameter (repeatable)       -s, --sweep N      -V, --variable NAME\n"
            "  -v, --verbose  -q, --quiet  --json  --config FILE  --print-command"
        ),
        epilog=(
            "examples:\n"
            f"  xrd info -i {SAMPLE}\n"
            f"  xrd plot -i {SAMPLE} -V velocity -p vel.png\n"
            f"  xrd run radarx_hid -i {SAMPLE} --all-sweeps -o results\n"
            "  xrd mosaic -i KHPX20211211_015357_V06 KNQA20211211_015018_V06 -o mosaic.nc -p map.png -v\n"
            "\n"
            "exit codes: 0 ok, 1 processing failed, 2 wrong usage, 3 input file problem,\n"
            "4 missing Python package, 70 internal error.   'xrd examples' shows more."
        ),
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)

    def command(name: str, summary: str, epilog: str, **kw: Any) -> argparse.ArgumentParser:
        return sub.add_parser(
            name,
            parents=[common],
            help=summary,
            description=summary,
            epilog=epilog,
            formatter_class=_Formatter,
            allow_abbrev=False,
            **kw,
        )

    # -- info ----------------------------------------------------------------------------
    p = command(
        "info",
        "Show format, radar, time, sweeps, variables and Nyquist velocity of radar files.",
        "examples:\n"
        f"  xrd info -i {SAMPLE}\n"
        "  xrd info -i KHPX20211211_015357_V06 KNQA20211211_015018_V06 --json\n"
        "  xrd info -i 'mayfield/*/*_V06'\n"
        "  xrd info -i mayfield/KHPX\n",
    )
    _add_input(p)
    p.set_defaults(func=cmd_info)

    # -- plot ----------------------------------------------------------------------------
    p = command(
        "plot",
        "Draw a PPI of one variable with the publication renderer (PNG, SVG or PDF).",
        "examples:\n"
        f"  xrd plot -i {SAMPLE}\n"
        f"  xrd plot -i {SAMPLE} -V velocity -p vel.png --theme dark\n"
        f"  xrd plot -i {SAMPLE} -V DBZH -s 2 --dpi 300 -p figure.pdf\n"
        "\n"
        "Without -p the picture is named after the file (KTLX..._DBZH_sweep0.png) and written to\n"
        "the current directory. Without -s the lowest sweep that has the variable is used.\n"
        "-p and -o are the same here.",
    )
    _add_input(p, "radar file")
    _add_variable(p, None, "variable")
    _add_sweep(p)
    _add_plot(p, "figure file .png/.svg/.pdf (default: named after the file)")
    _add_output(p, "same as -p")
    _add_format(p, "png, svg or pdf when the file name has no extension")
    p.add_argument("--dpi", type=int, default=200, help="resolution (default 200)")
    p.add_argument("--theme", choices=["light", "dark"], default="light")
    p.add_argument("--vmin", type=float, help="colour scale minimum")
    p.add_argument("--vmax", type=float, help="colour scale maximum")
    p.add_argument("--title", help="figure title")
    p.add_argument("--no-rings", action="store_true", help="omit the range rings")
    p.set_defaults(func=cmd_plot)

    # -- pipeline ------------------------------------------------------------------------
    p = command(
        "pipeline",
        "Apply a QC recipe. Subcommands: list, show RECIPE, run RECIPE (the default), script RECIPE.",
        "RECIPE is a built-in name, a recipe saved from the app, or the path of a recipe JSON file;\n"
        "a unique part of the name is enough. Name the recipe first, quote it if it has spaces.\n"
        "\n"
        "examples:\n"
        "  xrd pipeline list\n"
        '  xrd pipeline show "Reflectivity QC"\n'
        f'  xrd pipeline "Doppler velocity" -i {SAMPLE} --target VRADH -p before_after.png\n'
        f'  xrd pipeline run "Reflectivity QC" -i {SAMPLE} -o qc.nc\n'
        f'  xrd pipeline "Reflectivity QC" -i {SAMPLE} -P snr_gate.min_snr=5 -P gap_fill=off\n'
        '  xrd pipeline script "Reflectivity QC"\n'
        '  xrd pipeline script "Reflectivity QC" --api\n'
        "\n"
        "All sweeps are processed unless -s N is given. The result is one derived variable; without\n"
        "-o it is saved as <file>_<recipe>.nc in the current directory (otherwise it would be lost\n"
        "when the command ends). -P STEP.PARAMETER=VALUE changes a step setting; -P STEP=off disables\n"
        "a step. -o: .nc (the volume with the result), .json (statistics) or a folder; -p draws a\n"
        "before / after figure.",
    )
    p.add_argument(
        "args",
        nargs="*",
        metavar="RECIPE",
        help="recipe name, or: list | show RECIPE | run RECIPE | script RECIPE",
    )
    p.add_argument("-r", "--recipe", dest="recipe_opt", metavar="RECIPE", help=argparse.SUPPRESS)
    _add_input(p, "radar file")
    p.add_argument("--target", metavar="VAR", help="variable to process (default: the recipe's)")
    _add_sweep(p, all_sweeps=True)
    _add_param(p, "step setting STEP.PARAMETER=VALUE or STEP=on|off (repeatable)")
    _add_output(p, "result file (.nc / .json) or folder")
    _add_plot(p, "also write a before / after figure")
    _add_format(p, "comma-separated nc,json,png (for a folder; default nc,json)")
    p.add_argument(
        "--plot-sweep", type=int, metavar="N", help="sweep drawn in the figure (default: first)"
    )
    p.add_argument("--dpi", type=int, default=150, help="figure resolution (default 150)")
    p.add_argument(
        "--api", action="store_true", help="'script': print the short API snippet instead"
    )
    p.set_defaults(func=cmd_pipeline)

    # -- run -----------------------------------------------------------------------------
    p = command(
        "run",
        "Run an algorithm on a file: one sweep (-s N) or all sweeps (--all-sweeps). Same as 'modules run'.",
        "examples:\n"
        f"  xrd run storm_detection -i {SAMPLE} -P threshold_dbz=40\n"
        f"  xrd run radarx_hid -i {SAMPLE} -p hid.png\n"
        f"  xrd run radarx_shear -i {SAMPLE} -s 1 -o shear.nc\n"
        f"  xrd run radarx_hid -i {SAMPLE} --all-sweeps -o results -v\n"
        f"  xrd run storm_detection -i {SAMPLE} -o cells.csv\n"
        "\n"
        "Name the module first (list them with 'xrd modules'). Without -s the lowest sweep that has\n"
        "every field the module needs is used and printed (velocity is on sweep 1 of a NEXRAD\n"
        "volume). -o is a file (.json summary, .csv table, .nc fields, .png figure) or a folder (json,\n"
        "csv and nc; choose with -f json,csv,nc,png). With --all-sweeps the .nc is the whole volume\n"
        "plus the derived variable, and without -o it is saved as <file>_<module>_all.nc. -p draws\n"
        "the first field.",
    )
    p.add_argument("module", nargs="?", metavar="MODULE", help="module id (see 'xrd modules')")
    p.add_argument("-m", "--module", dest="module_opt", metavar="MODULE", help=argparse.SUPPRESS)
    _add_input(p, "radar file")
    _add_sweep(p, all_sweeps=True)
    _add_param(p, "module parameter (repeatable); names: 'xrd modules show MODULE'")
    _add_output(p, "result file or folder (see below)")
    _add_plot(p, "also write a PPI figure of the first field")
    _add_format(p, "comma-separated json,csv,nc,png (for a folder; default json,csv,nc)")
    p.add_argument(
        "--plot-sweep",
        type=int,
        metavar="N",
        help="with --all-sweeps: sweep drawn (default: first)",
    )
    p.add_argument("--dpi", type=int, default=150, help="figure resolution (default 150)")
    p.add_argument(
        "--timeout", type=float, default=None, metavar="S", help="stop the module after S seconds"
    )
    p.set_defaults(func=cmd_run)

    # -- modules -------------------------------------------------------------------------
    p = command(
        "modules",
        "List the algorithms ('modules list'), describe one ('modules show ID') or run one ('modules run').",
        "examples:\n"
        "  xrd modules\n"
        "  xrd modules list --category Dual-pol\n"
        "  xrd modules --json\n"
        "  xrd modules show radarx_hid\n"
        f"  xrd modules run radarx_hid -i {SAMPLE}\n",
    )
    p.add_argument("--category", metavar="C", help="only this category (e.g. QC, Dual-pol)")
    p.add_argument(
        "--available", action="store_true", help="hide modules whose Python package is missing"
    )
    p.add_argument("action", nargs="?", choices=["list", "show"], help="list (default) or show ID")
    p.add_argument("module_id", nargs="?", metavar="ID", help="module id for 'show'")
    p.set_defaults(func=cmd_modules)

    # -- cells ---------------------------------------------------------------------------
    p = command(
        "cells",
        "Find storm cells in one file, or track them through several files of one radar.",
        "examples:\n"
        f"  xrd cells -i {SAMPLE} --threshold-dbz 40 -o cells.csv\n"
        f"  xrd cells -i {SAMPLE} -p cells.png\n"
        "  xrd cells -i KHPX20211211_015357_V06 KHPX20211211_015930_V06 KHPX20211211_020457_V06 \\\n"
        "      -o cells.csv --tracks tracks.csv -p tracks.png\n"
        "  xrd cells -i 'mayfield/KHPX/*_V06' -p tracks.png\n"
        "\n"
        "-o: .csv (cell table), .json or .png; several files are tracked in time order.",
    )
    _add_input(p, "radar file(s); several = tracking")
    _add_sweep(p)
    _add_variable(p, "DBZH", "reflectivity field")
    p.add_argument("--threshold-dbz", type=float, default=35.0, help="cell threshold (35)")
    p.add_argument("--min-size-km2", type=float, default=10.0, help="smallest cell, km2 (10)")
    p.add_argument("--backend", choices=["auto", "radarx", "numpy"], default="auto")
    p.add_argument("--dt-seconds", type=float, help="time step between files (default: file times)")
    p.add_argument("--max-distance-km", type=float, default=30.0, help="link distance (30)")
    p.add_argument("--tracks", metavar="CSV", help="also write the track table (several files)")
    _add_output(p, "output .csv / .json / .png (repeatable)")
    _add_plot(p, "also write a figure (cell outlines, or tracks)")
    p.add_argument("--dpi", type=int, default=150, help="figure resolution (default 150)")
    p.set_defaults(func=cmd_cells)

    # -- sounding ------------------------------------------------------------------------
    p = command(
        "sounding",
        "Read a radiosonde file and print its thermodynamic and wind parameters, labelled.",
        "examples:\n"
        "  xrd sounding -i KJAN_2022033000.txt\n"
        "  xrd sounding -i BMX_20220330_00Z.csv --wind-units kt -v\n"
        "  xrd sounding -i sonde.csv --json\n"
        "  xrd sounding -i sonde.csv -o profile.csv\n"
        "\n"
        "Reads University of Wyoming text lists and CSV, SPC / SHARPpy text, IEM JSON, IGRA2 and any\n"
        "CSV whose header names the columns (units in the header are honoured). Every number says\n"
        "which parcel it is (surface-based, mixed-layer 100 hPa, most-unstable 300 hPa), its height\n"
        "reference (m AGL) and its definition (-v). A value that cannot be computed is n/a; a\n"
        "wind speed with no unit is read as m/s and the output says so (--wind-units kt).\n"
        "Needs radarx for the parcel thermodynamics (pip install radarx). Nothing is downloaded.",
    )
    _add_input(p, "sounding file")
    _add_format(p, "wyoming_text, sharppy, iem_json, csv or igra2 (default: detected)")
    p.add_argument(
        "--wind-units",
        metavar="UNIT",
        help="unit of a wind speed column without one in the header: m/s (default), kt, mph, km/h",
    )
    p.add_argument(
        "--no-virtual-temperature",
        action="store_true",
        help="leave out the virtual-temperature correction in CAPE/CIN (not recommended)",
    )
    _add_output(p, "write the full result (.json) or the profile levels (.csv); repeatable")
    p.set_defaults(func=cmd_sounding)

    # -- mosaic --------------------------------------------------------------------------
    p = command(
        "mosaic",
        "Merge several radars onto one map grid (needs the radarx package).",
        "examples:\n"
        "  xrd mosaic -i KHPX20211211_015357_V06 KNQA20211211_015018_V06\n"
        "  xrd mosaic -i KHPX20211211_015357_V06 KNQA20211211_015018_V06 -o mosaic.nc -p map.png -v\n"
        "  xrd mosaic -i KHPX20211211_015357_V06 KNQA20211211_015018_V06 KPAH20211211_015335_V06 -o mosaic.png\n"
        "  xrd mosaic -i KHPX20211211_015357_V06 KNQA20211211_015018_V06 --product cappi --height-m 2000 -o cappi.nc\n"
        "  xrd mosaic -i 'mayfield/*/*_015*_V06' -o m.nc -o m.png -P range_scale_km=60\n"
        "\n"
        "The map box, grid and weights are chosen from the radars and printed. Without -o the\n"
        "mosaic is saved as mosaic_<product>_<variable>_<time>.nc plus a map figure (.png).\n"
        "-o .nc is the gridded NetCDF; -o .png is the colour image plus its world file (.pgw) and\n"
        "projection (.prj), so GIS software places it. -p is a map figure with the radar sites.\n"
        "-P passes further engine options (KEY=VALUE), e.g. range_scale_km, time_scale_s, calibrate.",
    )
    _add_input(p, "radar files of one time")
    p.add_argument(
        "--product",
        choices=["composite", "cappi", "echo_top"],
        default="composite",
        help="composite = column maximum (default), cappi = constant altitude, echo_top",
    )
    _add_variable(p, "DBZH", "field to mosaic")
    p.add_argument(
        "--height-m", type=float, default=3000.0, help="CAPPI height above sea level (3000)"
    )
    p.add_argument("--resolution-m", type=float, default=1000.0, help="cell size, 250-5000 (1000)")
    p.add_argument("--max-range-km", type=float, help="ignore data beyond this range")
    p.add_argument(
        "--extent", metavar="W,S,E,N", help="map box in degrees (default: covers all radars)"
    )
    p.add_argument("--threshold-dbz", type=float, help="echo_top threshold (default 18)")
    p.add_argument("--vmin", type=float, help="colour scale minimum")
    p.add_argument("--vmax", type=float, help="colour scale maximum")
    p.add_argument(
        "--no-merge", action="store_true", help="first radar wins in overlaps (no weighted merge)"
    )
    _add_param(p, "further engine option KEY=VALUE (repeatable)")
    _add_output(p, "output .nc or .png (repeatable)")
    _add_plot(p, "also write a map figure with the radar sites")
    p.add_argument("--dpi", type=int, default=150, help="figure resolution (default 150)")
    p.set_defaults(func=cmd_mosaic)

    # -- winds ---------------------------------------------------------------------------
    p = command(
        "winds",
        "Retrieve 3-D winds from two or more radars (multi-Doppler; needs the radarx package).",
        "examples:\n"
        "  xrd winds -i KHPX20211211_015357_V06 KNQA20211211_015018_V06\n"
        "  xrd winds -i KHPX20211211_015357_V06 KNQA20211211_015018_V06 KPAH20211211_015335_V06 \\\n"
        "      --method variational -o winds.nc -p winds.png --level-km 4.5 -v\n"
        "  xrd winds -i KHPX20211211_015357_V06 KNQA20211211_015018_V06 --extent 80 \\\n"
        "      --origin 36.5,-88.0 --dz-m 500 -P min_crossing_angle=25 -o winds.nc\n"
        "\n"
        "Origin, box and grid are chosen from the radar positions and printed. Without -o the\n"
        "analysis is saved as winds_<time>.nc plus a map (.png). --extent is the half-width in km\n"
        "of the analysis box around the origin. -P passes any further engine option (KEY=VALUE).\n"
        "-o: .nc (analysis) or .json (description); -p: map of the level given by --level-km\n"
        "(default: best covered), coloured by --field.",
    )
    _add_input(p, "two or more radar files of one time")
    p.add_argument("--method", choices=["analytic", "variational"], default="analytic")
    p.add_argument(
        "--dealias",
        choices=["auto", "always", "never"],
        default="auto",
        help="velocity unfolding (default auto; needs an engine version with dealias support)",
    )
    p.add_argument(
        "--origin", metavar="LAT,LON", help="grid origin (default: centre of the radars)"
    )
    p.add_argument("--extent", type=float, metavar="KM", help="half-width of the analysis box, km")
    p.add_argument("--dx-m", type=float, help="horizontal grid spacing, metres (1000)")
    p.add_argument("--dz-m", type=float, help="vertical grid spacing, metres (500)")
    p.add_argument("--zmin-m", type=float, help="lowest level, metres above sea level")
    p.add_argument("--zmax-m", type=float, help="highest level, metres above sea level (10000)")
    p.add_argument("--max-range-km", type=float, help="ignore data beyond this range (100)")
    _add_param(p, "further engine option KEY=VALUE (repeatable)")
    _add_output(p, "output .nc (analysis) or .json (repeatable)")
    _add_plot(p, "also write a map of one level (see --level-km)")
    p.add_argument("--level-km", type=float, help="level (km above sea level) for -p")
    p.add_argument("--field", default="speed", help="field shown in colour (default speed)")
    p.add_argument("--dpi", type=int, default=150, help="figure resolution (default 150)")
    p.set_defaults(func=cmd_winds)

    # -- export --------------------------------------------------------------------------
    p = command(
        "export",
        "Write a volume (or one sweep / variable) as NetCDF, CfRadial2, Zarr, CSV or an image.",
        "examples:\n"
        f"  xrd export -i {SAMPLE} -o ktlx.nc\n"
        f"  xrd export -i {SAMPLE} -o dbz.csv -V DBZH -s 0\n"
        f"  xrd export -i {SAMPLE} -o ktlx.zarr\n"
        f"  xrd export -i {SAMPLE} -f cfradial2 -o ktlx_cf2.nc\n"
        f'  xrd export -i {SAMPLE} -o qc.nc --pipeline "Reflectivity QC" \\\n'
        "      --run radarx_hid --include-derived\n"
        "\n"
        "The format follows the -o extension (.nc netcdf, .zarr, .csv, .png, .svg, .pdf); use -f\n"
        "for cfradial2. Without -o and -f the file is a NetCDF named after the input. Derived\n"
        "variables only exist inside one run: compute them here with --pipeline RECIPE and\n"
        "--run MODULE (repeatable; all sweeps) and add them with --include-derived.",
    )
    _add_input(p, "radar file")
    _add_output(p, "output file (default: named after the input)")
    _add_format(p, "netcdf, cfradial2, zarr, csv, png, svg or pdf (default: from the -o extension)")
    _add_variable(p, None, "variable for csv / images")
    _add_sweep(p)
    p.add_argument(
        "--include-derived",
        action="store_true",
        help="add the derived variables of --pipeline / --run to the file",
    )
    p.add_argument(
        "--pipeline", action="append", default=[], metavar="RECIPE", help="apply a recipe first"
    )
    p.add_argument(
        "--run",
        action="append",
        default=[],
        metavar="MODULE[:K=V,...]",
        help="run a module on all sweeps first (repeatable)",
    )
    p.add_argument("--dpi", type=int, default=300, help="image resolution (default 300)")
    p.set_defaults(func=cmd_export)

    # -- examples / completion -----------------------------------------------------------
    p = command(
        "examples",
        "Print a guided tour of copy-paste commands ('xrd examples TOPIC' for one part).",
        "topics: " + ", ".join(TOPICS) + "\nexample:\n  xrd examples mosaic\n",
    )
    p.add_argument("topic", nargs="?", metavar="TOPIC", help="one of: " + ", ".join(TOPICS))
    p.set_defaults(func=cmd_examples)

    p = command(
        "completion",
        "Print a tab-completion script for bash, zsh or fish.",
        "examples:\n"
        "  xrd completion bash > ~/.local/share/bash-completion/completions/xrd\n"
        "  source <(xrd completion zsh)\n"
        "  xrd completion fish > ~/.config/fish/completions/xrd.fish\n",
    )
    p.add_argument("shell", choices=["bash", "zsh", "fish"], help="your shell")
    p.set_defaults(func=cmd_completion)

    return parser


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------


def _split_kv(items: Sequence[str], flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item or item.startswith("="):
            raise UsageError(
                f"{flag} {item!r} is not KEY=VALUE (for example {flag} threshold_dbz=40)"
            )
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _numbers(text: str, n: int, label: str) -> list[float]:
    try:
        vals = [float(v) for v in text.split(",")]
    except ValueError:
        vals = []
    if len(vals) != n:
        raise UsageError(f"{label} must be {n} comma-separated numbers, got {text!r}")
    return vals


def _json_or_text(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value


def expand_inputs(items: Sequence[str]) -> list[str]:
    """Turn what the user typed after ``-i`` into file names.

    ``-`` reads names from stdin (one per line, ``#`` comments allowed), a folder becomes the
    radar files inside it, a pattern with ``* ? [`` is globbed (and must match something), and
    anything else is taken as it is. Order is kept and duplicates are dropped.
    """
    from engine.api_results import XrdInputError

    out: list[str] = []
    for raw in items:
        if raw == "-":
            lines = [ln.strip() for ln in sys.stdin.read().splitlines()]
            out += expand_inputs([ln for ln in lines if ln and not ln.startswith("#")])
            continue
        path = os.path.expanduser(raw)
        if os.path.isdir(path):
            found = sorted(
                os.path.join(path, name)
                for name in os.listdir(path)
                if not name.startswith(".")
                and os.path.isfile(os.path.join(path, name))
                and Path(name).suffix.lower() not in NON_RADAR_SUFFIXES
            )
            if not found:
                raise XrdInputError(
                    f"No radar files in folder {raw}. Give files, or a folder that holds them."
                )
            out += found
        elif any(ch in path for ch in "*?["):
            matches = sorted(glob.glob(path))
            if not matches:
                raise XrdInputError(
                    f"The pattern {raw!r} matches no files (checked from {os.getcwd()}). "
                    "Quote the pattern so that xrd, not the shell, expands it."
                )
            out += [m for m in matches if os.path.isfile(m)]
        else:
            out.append(raw)
    seen: set[str] = set()
    return [f for f in out if not (f in seen or seen.add(f))]


def _inputs(args: argparse.Namespace, *, minimum: int = 1, maximum: int | None = None) -> list[str]:
    """Input files from ``-i`` plus bare arguments, with globs / folders / stdin expanded."""
    files = expand_inputs([*args.input, *getattr(args, "extra", [])])
    cmd = args.command
    if len(files) < minimum:
        need = "at least two radar files" if minimum > 1 else "a radar file"
        example = "-i FILE1 FILE2 [FILE3 ...]" if minimum > 1 else f"-i {SAMPLE}"
        raise UsageError(
            f"{cmd} needs {need}: give them with -i (for example: xrd {cmd} {example})"
            + (f" - only {len(files)} given" if files else "")
        )
    if maximum is not None and len(files) > maximum:
        raise UsageError(
            f"{cmd} takes {'one input file' if maximum == 1 else f'at most {maximum} files'}; "
            f"got {len(files)}: {' '.join(files[:4])}{' ...' if len(files) > 4 else ''}. "
            f"Run it once per file, or use 'xrd {cmd} --help'."
        )
    return files


def _formats(text: str | None, allowed: Sequence[str]) -> list[str] | None:
    """``--format a,b`` as a list (``netcdf`` = ``nc``); ``None`` when not given."""
    if not text:
        return None
    out: list[str] = []
    for part in text.split(","):
        part = part.strip().lower()
        part = "nc" if part == "netcdf" else part
        if part not in allowed:
            raise UsageError(f"--format {part!r} is not one of: {', '.join(allowed)}")
        if part not in out:
            out.append(part)
    return out


def _open(path: str, mapping: dict[str, str] | None = None) -> Any:
    from engine import api

    return api.open(path, mapping=mapping)


def _save_plot(fig: Any, path: str | Path, dpi: int, out: Out) -> None:
    from engine.api_results import save_figure

    out.wrote(save_figure(fig, path, dpi))


def _stem(path: str) -> str:
    return Path(path).name.split(".")[0] or "radar"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower() or "x"


def _safe(text: str) -> str:
    """A name that is safe in a file name, keeping its case (``radarx_hid:class`` -> ``radarx_hid_class``)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "x"


def _time_tag(*times: str | None) -> str:
    """``2021-12-11T02:02:27Z`` -> ``20211211T0202`` (the latest of the given times)."""
    stamps = sorted(t for t in times if t)
    if not stamps:
        return time.strftime("%Y%m%dT%H%M")
    return re.sub(r"[^0-9T]", "", stamps[-1])[:13]


def _need_sweep(out: Out, vol: Any, requires: Sequence[str], given: int | None) -> int:
    """The sweep to use: the given one, or the lowest that has *requires* (announced)."""
    if given is not None:
        return given
    sweep = vol.default_sweep(list(requires))
    if sweep != 0 and requires:
        out.chose(
            f"sweep {sweep} (the lowest sweep with {', '.join(requires)}; choose with -s N)",
            "-s",
            str(sweep),
        )
    return sweep


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def cmd_info(args: argparse.Namespace, out: Out) -> int:
    infos = []
    for path in _inputs(args):
        with _open(path) as vol:
            infos.append(vol.info())
    if out.json:
        out.emit_json(infos[0] if len(infos) == 1 else infos)
        return EXIT_OK
    for i, info in enumerate(infos):
        if i:
            out.result("")
        out.result(_format_info(info))
    return EXIT_OK


def _format_info(info: dict[str, Any]) -> str:
    lat, lon = info.get("latitude"), info.get("longitude")
    where = f" at {lat:.4f}, {lon:.4f}" if lat is not None and lon is not None else ""
    alt = f", {info['altitude_m']:.0f} m" if info.get("altitude_m") is not None else ""
    lines = [
        bold(Path(info["path"]).name if info.get("path") else info["name"]),
        f"  format     {info['format']}",
        f"  radar      {info['radar']}{where}{alt}",
        f"  time       {info['time'] or 'unknown'}",
        f"  size       {_human_bytes(info['size_bytes'])} on disk, "
        f"{_human_bytes(info['memory_bytes'])} decoded",
        f"  variables  {' '.join(info['variables'])}",
        f"  sweeps     {info['n_sweeps']}",
    ]
    rows = []
    for s in info["sweeps"]:
        elev = s["elevation"]
        rows.append(
            (
                s["index"],
                f"{elev:.2f}" if elev is not None else "-",
                s["mode"].replace("azimuth_", "").replace("_", " ") or "-",
                s["n_rays"],
                s["n_gates"],
                f"{s['max_range_km']:.0f}" if s["max_range_km"] is not None else "-",
                _nyquist_text(s),
                " ".join(s["variables"]),
            )
        )
    lines.append(
        _table(
            ("idx", "elev", "mode", "rays", "gates", "range_km", "nyquist", "variables"),
            rows,
            "    ",
        )
    )
    if any(s["nyquist_estimated_ms"] for s in info["sweeps"]):
        lines.append(
            "    (~ = estimated from the largest velocity in the sweep: the file has no Nyquist value)"
        )
    return "\n".join(lines)


def _nyquist_text(sweep: dict[str, Any]) -> str:
    if sweep["nyquist_ms"]:
        return f"{sweep['nyquist_ms']:.1f}"
    if sweep.get("nyquist_estimated_ms"):
        return f"~{sweep['nyquist_estimated_ms']:.1f}"
    return "-"


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------


def cmd_modules(args: argparse.Namespace, out: Out) -> int:
    from engine import api

    if args.action == "show":
        if not args.module_id:
            raise UsageError("'modules show' needs a module id, e.g. xrd modules show radarx_hid")
        info = api.module(args.module_id)
        if out.json:
            out.emit_json(dict(info))
        else:
            out.result(api.format_module(info))
            out.result(
                f"\nRun it:  xrd run {info['id']} -i FILE"
                + (" --all-sweeps" if info["per_sweep"] else "")
            )
        return EXIT_OK
    if args.module_id:
        raise UsageError("unexpected argument; use 'xrd modules show ID' to describe a module")
    listing = api.modules(args.category, available_only=args.available)
    if out.json:
        out.emit_json({"modules": list(listing), "errors": _load_errors()})
        return EXIT_OK
    rows = [
        (
            m["id"],
            m["category"],
            m["scope"],
            ", ".join(m["requires"]) or "-",
            m["title"] + ("" if m["available"] else "  [needs " + ", ".join(m["needs"]) + "]"),
        )
        for m in listing
    ]
    out.result(_table(("id", "category", "scope", "requires", "title"), rows))
    out.result(
        f"\n{len(rows)} module(s).  Describe one: xrd modules show ID     Run one: xrd run ID -i FILE"
    )
    for err in _load_errors():
        out.warn(f"module file {err['file']} could not be loaded: {err['error']}")
    return EXIT_OK


def _load_errors() -> list[dict[str, str]]:
    from engine import plugins

    return list(plugins.LOAD_ERRORS)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, out: Out) -> int:
    from engine import api

    module = args.module_opt or args.module
    if not module:
        raise UsageError("run needs a module: xrd run MODULE -i FILE (list them with: xrd modules)")
    path = _inputs(args, maximum=1)[0]
    spec = api.find_module(module)
    params = api.check_params(spec, _split_kv(args.param, "-P"))
    formats = _formats(args.format, ("json", "csv", "nc", "png"))
    with _open(path) as vol:
        sweep = None if args.all_sweeps else _need_sweep(out, vol, spec.requires, args.sweep)
        res = vol.run(
            spec.id,
            sweep,
            all_sweeps=args.all_sweeps,
            params=params,
            progress=out.progress,
            timeout_s=args.timeout if args.timeout is not None else "env",
        )
        out.progress_done()
        if not out.json:
            _print_run(res, path, out)
        outputs = list(args.output)
        if not outputs and not formats and res.sweep is None and not args.plot:
            outputs = [
                f"{_stem(path)}_{res.module}_all.nc"
            ]  # derived variables only live in a file
            out.chose(f"{outputs[0]} as the output (add -o to choose)", "-o", outputs[0])
        elif not outputs and formats:
            outputs = ["."]
        out.wrote(*res.save(outputs, formats, sweep=args.plot_sweep, dpi=args.dpi))
        notes = list(res.save_notes)
        if args.plot:
            if res.fields or res.derived:
                fig = res.plot(sweep=args.plot_sweep) if res.sweep is None else res.plot()
                _save_plot(fig, args.plot, args.dpi, out)
            else:
                notes.append(f"{res.module} produced no gridded field; no figure written")
        for note in notes:
            out.warn(note)
    if out.json:
        payload = res.to_dict()
        payload["files"] = out.files
        out.emit_json(payload)
    elif not (outputs or args.plot) and not out.quiet:
        out.note("(nothing saved: add -o FILE to keep the result, -p FILE.png for a picture)")
    return EXIT_OK


def _shorten(text: str, n: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _reason(text: str, full: bool) -> str:
    from engine.api import simplify_reason

    text = simplify_reason(text)
    return text if full else _shorten(text)


def _print_run(res: Any, file: str, out: Out) -> None:
    if out.quiet:
        return
    full = out.verbose > 0
    where = "all sweeps" if res.sweep is None else f"sweep {res.sweep}"
    print(
        bold(f"{res.module}  {res.title}")
        + f"  |  {Path(file).name}  |  {where}  |  {_seconds(res.elapsed_ms)}"
    )
    if res.params and (full or res.sweep is None):
        print("  parameters: " + ", ".join(f"{k}={v}" for k, v in res.params.items()))
    if res.sweep is None:
        print(f"  derived variable(s): {', '.join(res.derived) or 'none'}")
        print(f"  processed {len(res.summaries)} sweep(s), skipped {len(res.skipped)}")
        for s in res.skipped:
            reason = _reason(s["reason"], full)
            print(f"    skipped sweep {s['sweep']}: {reason}")
        if res.summary.shape[0]:
            print("  per-sweep summary:")
            print(
                res.summary.to_string(index=False, max_rows=None if full else 20, max_colwidth=28)
            )
        return
    for k, v in res.scalars.items():
        if k == next(iter(res.scalars)):
            print("  summary")
        print(f"    {k}: {f'{v:.6g}' if isinstance(v, float) else v}")
    for title, df in res.tables.items():
        print(f"  table: {title}")
        if len(df):
            text = df.to_string(index=False, max_rows=None if full else 25, max_colwidth=30)
            print("\n".join("    " + line for line in text.splitlines()))
    for name, da in res.fields.items():
        valid = float(da.notnull().mean()) * 100.0 if da.size else 0.0
        units = f" [{da.attrs.get('units')}]" if da.attrs.get("units") else ""
        print(f"  field: {name}{units}  {da.shape[0]}x{da.shape[1]}, {valid:.1f}% valid")
    for note in res.notes:
        print(f"  note: {note}")
    if res.text and full:
        print("  text: " + res.text.replace("\n", "\n         "))
    elif res.text:
        print("  (explanation of the method: add -v)")
    for w in res.warnings:
        print(f"  {_warn_label(sys.stdout)} {w}")


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def cmd_pipeline(args: argparse.Namespace, out: Out) -> int:
    from engine import api

    words = [*args.args, *getattr(args, "recipe_words", [])]
    if args.recipe_opt:
        words = [args.recipe_opt, *words]
    if not words:
        raise UsageError(
            "pipeline needs a RECIPE: xrd pipeline RECIPE -i FILE   (see them with: xrd pipeline list)"
        )
    action = words[0] if words[0] in ("list", "show", "run", "script") else "run"
    if action != "run" or words[0] == "run":
        words = words[1:]
    if action == "list":
        return _pipeline_list(api, out)
    if action in ("show", "script"):
        if len(words) != 1:
            raise UsageError(
                f"usage: xrd pipeline {action} RECIPE" + (" [--api]" if action == "script" else "")
            )
        if action == "show":
            return _pipeline_show(api, words[0], out)
        if args.api:
            print(_api_snippet(api.recipe(words[0])))
        else:
            print(api.recipe_script(words[0]), end="")
        return EXIT_OK
    if not words:
        raise UsageError("pipeline run needs a RECIPE: xrd pipeline run RECIPE -i FILE")
    args.extra = [*words[1:], *args.extra]  # RECIPE FILE: the bare words after the recipe
    path = _inputs(args, maximum=1)[0]
    formats = _formats(args.format, ("nc", "json", "png"))
    step_params = _split_kv(args.param, "-P")
    with _open(path) as vol:
        res = vol.pipeline(
            words[0],
            args.target,
            sweep=args.sweep,
            all_sweeps=args.all_sweeps or None,
            params=step_params or None,
            progress=out.progress,
        )
        out.progress_done()
        if not out.json:
            _print_pipeline(res, path, out)
        outputs = list(args.output)
        if not outputs and not formats:
            outputs = [f"{_stem(path)}_{_slug(res.recipe['name'])[:40]}.nc"]
            out.chose(f"{outputs[0]} as the output (add -o to choose)", "-o", outputs[0])
        elif not outputs:
            outputs = ["."]
        out.wrote(*res.save(outputs, formats, sweep=args.plot_sweep, dpi=args.dpi))
        if args.plot:
            fig_sweep = args.plot_sweep if args.plot_sweep is not None else res.sweeps[0]
            _save_plot(res.plot(fig_sweep), args.plot, args.dpi, out)
    if out.json:
        payload = res.to_dict()
        payload["files"] = out.files
        out.emit_json(payload)
    return EXIT_OK


def _pipeline_list(api: Any, out: Out) -> int:
    listing = api.recipes()
    if out.json:
        out.emit_json({"recipes": list(listing)})
        return EXIT_OK
    rows = [
        (
            r["name"],
            r["target"],
            "built-in" if r.get("builtin") else "saved",
            " > ".join(s["step_id"] for s in r["steps"] if s.get("enabled", True)),
        )
        for r in listing
    ]
    out.result(_table(("recipe", "target", "kind", "steps"), rows))
    out.result(
        "\nSteps of one recipe: xrd pipeline show RECIPE     Apply: xrd pipeline RECIPE -i FILE"
    )
    return EXIT_OK


def _pipeline_show(api: Any, name: str, out: Out) -> int:
    from engine import pipeline as pipe

    rec = api.recipe(name)
    data = rec.to_dict()
    if out.json:
        out.emit_json(data)
        return EXIT_OK
    out.result(
        bold(rec.name) + f"   target {rec.target}   {'built-in' if rec.builtin else 'saved'}"
    )
    if rec.description:
        out.result(f"  {rec.description}")
    rows = []
    for i, st in enumerate(rec.steps, start=1):
        spec = pipe.get_step(st["step_id"])
        settings = ", ".join(f"{k}={v}" for k, v in st["params"].items()) or "-"
        rows.append(
            (i, st["step_id"], "on" if st.get("enabled", True) else "off", spec.title, settings)
        )
    out.result(_table(("#", "step", "state", "title", "settings"), rows))
    out.result(
        f"\nChange a setting: -P STEP.PARAMETER=VALUE (or -P STEP=off)     Apply: xrd pipeline {shlex.quote(rec.name)} -i FILE"
    )
    return EXIT_OK


def _print_pipeline(res: Any, file: str, out: Out) -> None:
    if out.quiet:
        return
    rec = res.recipe
    print(
        bold(f"{rec['name']}")
        + f"  |  {Path(file).name}  |  target {rec['target']}  |  {len(res.sweeps)} sweep(s)  |  "
        + _seconds(res.elapsed_ms)
    )
    rows = [
        (
            int(r["index"]) + 1,
            r["title"],
            r["status"],
            int(r["removed"]),
            int(r["modified"]),
            int(r["filled"]),
            f"{r['valid_pct']:.1f}",
            _shorten(r["reason"], 200 if out.verbose else 40),
        )
        for r in res.steps.to_dict("records")
        if r["status"] != "disabled"
    ]
    print(_table(("#", "step", "status", "removed", "modified", "filled", "valid%", "note"), rows))
    print(f"  derived variable: {res.name}")
    for s in res.skipped:
        reason = _reason(s["reason"], bool(out.verbose))
        print(f"  skipped sweep {s['sweep']}: {reason}")


def _api_snippet(recipe: Any) -> str:
    return (
        "from engine import api\n\n"
        'vol = api.open("FILE")\n'
        f"res = vol.pipeline({recipe.name!r}, target={recipe.target!r}, all_sweeps=True)\n"
        "print(res.steps)                       # what each step removed\n"
        "res.plot().savefig('before_after.png')\n"
        'vol.export("netcdf", "out.nc", include_derived=True)\n'
    )


# ---------------------------------------------------------------------------
# mosaic
# ---------------------------------------------------------------------------


def cmd_mosaic(args: argparse.Namespace, out: Out) -> int:
    from engine import api

    files = _inputs(args)
    for target in args.output:
        if Path(target).suffix.lower() not in (".png", ".nc"):
            raise UsageError(f"-o {target!r}: use .nc (NetCDF) or .png (picture + world file)")
    options: dict[str, Any] = {k: _json_or_text(v) for k, v in _split_kv(args.param, "-P").items()}
    for key in ("threshold_dbz", "vmin", "vmax"):
        if getattr(args, key) is not None:
            options[key] = getattr(args, key)
    extent = _numbers(args.extent, 4, "--extent") if args.extent else None
    mos = api.mosaic(
        files,
        product=args.product,
        variable=args.variable,
        resolution_m=args.resolution_m,
        height_m=args.height_m,
        extent=extent,
        max_range_km=args.max_range_km,
        merge=not args.no_merge,
        progress=out.progress,
        **options,
    )
    out.progress_done()
    if not out.json:
        _print_mosaic(mos, out, auto_extent=extent is None)
    outputs, plot = list(args.output), args.plot
    if not outputs and not plot:
        stem = f"mosaic_{args.product}_{_safe(args.variable)}_{_time_tag(mos.analysis_time)}"
        outputs, plot = [f"{stem}.nc"], f"{stem}_map.png"
        out.chose(
            f"{stem}.nc and {stem}_map.png as the outputs (add -o / -p to choose)",
            "-o",
            outputs[0],
            "-p",
            plot,
        )
    out.wrote(*mos.save(outputs))
    if plot:
        _save_plot(mos.plot(), plot, args.dpi, out)
    if out.json:
        payload = mos.to_dict()
        payload["files"] = out.files
        out.emit_json(payload)
    return EXIT_OK


def _print_mosaic(mos: Any, out: Out, auto_extent: bool) -> None:
    if out.quiet:
        return
    ny, nx = mos.image.shape
    height = (
        f" at {mos.height_m:g} m" if mos.product == "cappi" and mos.height_m is not None else ""
    )
    grid = mos.header().get("grid", {})
    print(
        bold(f"{mos.product} of {mos.variable}{height}")
        + f"  |  {len(mos.radars)} radar(s)  |  {nx} x {ny} cells @ {grid.get('resolution_m', '?')} m"
    )
    west, south, east, north = mos.bbox
    chosen = "  (chosen to cover all radars; --extent W,S,E,N to change)" if auto_extent else ""
    print(f"  extent  W {west:.3f}  S {south:.3f}  E {east:.3f}  N {north:.3f}  (degrees){chosen}")
    st = mos.stats
    if st:
        valid = st.get("valid_fraction")
        shown = "  ".join(
            f"{k} {v:.1f}" for k, v in st.items() if v is not None and k != "valid_fraction"
        )
        share = f"valid {100 * valid:.0f}% of cells  " if valid is not None else ""
        print(f"  values  {share}{shown} {mos.units}")
    rows = [
        (
            r["name"],
            f"{r['latitude']:.3f}",
            f"{r['longitude']:.3f}",
            r["volume_time"] or "-",
            f"{100 * r['coverage_fraction']:.0f}",
            f"{100 * r['share']:.0f}",
        )
        for r in mos.radars
    ]
    print(_table(("radar", "lat", "lon", "volume time", "coverage%", "share%"), rows, "  "))
    for w in mos.warnings:
        print(f"  {_warn_label(sys.stdout)} {w}")


# ---------------------------------------------------------------------------
# winds
# ---------------------------------------------------------------------------


def cmd_winds(args: argparse.Namespace, out: Out) -> int:
    from engine import api

    files = _inputs(args, minimum=2)
    for target in args.output:
        if Path(target).suffix.lower() not in (".nc", ".json", ".png", ".svg", ".pdf"):
            raise UsageError(f"-o {target!r}: use .nc (analysis), .json or .png (map)")
    origin = tuple(_numbers(args.origin, 2, "--origin")) if args.origin else None
    options = {k: _json_or_text(v) for k, v in _split_kv(args.param, "-P").items()}
    if args.level_km is not None and not (
        args.plot or any(o.endswith(".png") for o in args.output)
    ):
        raise UsageError("--level-km needs a figure: add -p winds.png")
    w = api.winds(
        files,
        method=args.method,
        dealias=args.dealias,
        extent=args.extent,
        origin=origin,
        dx_m=args.dx_m,
        dz_m=args.dz_m,
        z_min_m=args.zmin_m,
        z_max_m=args.zmax_m,
        max_range_km=args.max_range_km,
        progress=out.progress,
        **options,
    )
    out.progress_done()
    info = w.to_dict()
    if not out.json:
        _print_winds(w, info, out, auto_origin=origin is None)
    outputs, plot = list(args.output), args.plot
    if not outputs and not plot:
        times = [str(r.get("time") or r.get("volume_time") or "") for r in w.radars]
        stem = f"winds_{args.method}_{_time_tag(*times)}"
        outputs, plot = [f"{stem}.nc"], f"{stem}.png"
        out.chose(
            f"{stem}.nc and {stem}.png as the outputs (add -o / -p to choose)",
            "-o",
            outputs[0],
            "-p",
            plot,
        )
    out.wrote(*w.save(outputs, args.level_km, args.field, args.dpi))
    if plot:
        _save_plot(w.plot(args.level_km, args.field), plot, args.dpi, out)
    if out.json:
        info["files"] = out.files
        out.emit_json(info)
    return EXIT_OK


def _print_winds(w: Any, info: dict[str, Any], out: Out, auto_origin: bool) -> None:
    if out.quiet:
        return
    g = info.get("grid", {})
    print(
        bold(f"{info.get('method', 'wind')} winds")
        + f"  |  {len(w.radars)} radar(s)  |  grid {g.get('nx')} x {g.get('ny')} x {g.get('nz')}"
        + f" (dx {g.get('dx', 0):.0f} m)"
    )
    chosen = "  (centre of the radars; --origin LAT,LON to change)" if auto_origin else ""
    print(f"  origin  {g.get('origin_lat'):.4f}, {g.get('origin_lon'):.4f}{chosen}")
    zs = g.get("z") or []
    if zs:
        print(f"  levels  {zs[0] / 1000:.2f} to {zs[-1] / 1000:.2f} km above sea level ({len(zs)})")
    for r in w.radars:
        print(f"  radar   {r.get('name')}  {r.get('time') or r.get('volume_time') or ''}")
    for key in ("stats", "coverage", "dual_fraction", "valid_fraction"):
        if key in info:
            print(f"  {key}: {info[key]}")
    for note in w.warnings:
        print(f"  {_warn_label(sys.stdout)} {note}")


# ---------------------------------------------------------------------------
# cells
# ---------------------------------------------------------------------------


def cmd_cells(args: argparse.Namespace, out: Out) -> int:
    from engine import api

    files = _inputs(args)
    if len(files) == 1:
        with _open(files[0]) as vol:
            sweep = _need_sweep(out, vol, [args.variable], args.sweep)
            res = vol.cells(
                sweep,
                args.variable,
                args.threshold_dbz,
                args.min_size_km2,
                backend=args.backend,
            )
        if not out.json:
            _print_cells(res, files[0], out)
        out.wrote(*res.save(args.output, args.dpi))
        if args.plot:
            _save_plot(res.plot(), args.plot, args.dpi, out)
        if out.json:
            payload = res.to_dict()
            payload["file"] = files[0]
            payload["files"] = out.files
            out.emit_json(payload)
        return EXIT_OK
    trk = api.track(
        files,
        args.variable,
        args.sweep,
        args.threshold_dbz,
        args.min_size_km2,
        dt_seconds=args.dt_seconds,
        max_distance_km=args.max_distance_km,
        backend=args.backend,
    )
    if not out.json:
        _print_tracks(trk, out)
    out.wrote(*trk.save(args.output, args.tracks, args.dpi))
    if args.plot:
        _save_plot(trk.plot(), args.plot, args.dpi, out)
    if out.json:
        payload = trk.to_dict()
        payload["files"] = out.files
        out.emit_json(payload)
    return EXIT_OK


def _print_tracks(trk: Any, out: Out) -> None:
    if out.quiet:
        return
    cells_df, tracks_df = trk.cells_dataframe(), trk.to_dataframe()
    print(
        bold(f"{len(trk)} track(s)")
        + f"  |  {len(trk.scans)} scans  |  {len(cells_df)} cells  |  backend {trk.backend}"
    )
    if trk.motion is not None:
        from engine import cells as cell_engine

        speed, from_deg = cell_engine.uv_to_speed_dir(*trk.motion)
        print(f"  storm motion: {speed:.1f} m/s from {from_deg:.0f} deg")
    if trk.note:
        print(f"  note: {trk.note}")
    if len(tracks_df):
        cols = ["track_id", "lifetime", "speed_mps", "direction_deg", "status", "max_dbz"]
        print(tracks_df[cols].to_string(index=False, max_rows=None if out.verbose else 30))


def _print_cells(res: Any, file: str, out: Out) -> None:
    if out.quiet:
        return
    df = res.to_dataframe()
    print(
        bold(f"{len(res)} cell(s)")
        + f"  |  {Path(file).name}  |  {res.variable} sweep {res.sweep}  |  >= {res.threshold_dbz:g} dBZ, "
        f">= {res.min_size_km2:g} km2  |  backend {res.backend}"
    )
    if res.note:
        print(f"  note: {res.note}")
    if len(df):
        cols = [
            c
            for c in ("id", "lat", "lon", "centroid_range_km", "area_km2", "max_dbz", "mean_dbz")
            if c in df
        ]
        print(df[cols].to_string(index=False, max_rows=None if out.verbose else 25))


# ---------------------------------------------------------------------------
# plot
# ---------------------------------------------------------------------------


def cmd_sounding(args: argparse.Namespace, out: Out) -> int:
    from engine import provenance, sounding
    from engine.api_results import XrdDependencyError, XrdInputError

    (path,) = _inputs(args, maximum=1)
    try:
        snd = sounding.read_sounding_file(path, (args.format or "auto").lower(), args.wind_units)
        result = sounding.analyse(snd)
        if args.no_virtual_temperature:
            result["diagnostics"] = sounding.diagnose(snd, virtual=False)
    except sounding.SoundingError as exc:
        raise XrdInputError(f"{Path(path).name}: {exc}") from exc
    except sounding.RadarxMissing as exc:
        raise XrdDependencyError(str(exc)) from exc
    diag = result["diagnostics"]
    for target in args.output:
        suffix = Path(target).suffix.lower()
        if suffix == ".json":
            Path(target).write_text(dumps(result) + "\n", encoding="utf-8")
        elif suffix == ".csv":
            lv = snd.levels(None)
            lines = [
                provenance.csv_comment_block(
                    title=f"Sounding {snd.station or ''} {snd.launch_time or ''}".strip(),
                    columns={
                        "pressure_hpa": "hPa",
                        "height_msl_m": "m above sea level (geometric)",
                        "temperature_c": "degC",
                        "dewpoint_c": "degC (reported values only)",
                        "u_ms": "m s-1",
                        "v_ms": "m s-1",
                    },
                    source_files=[Path(path).name],
                    layer=result["provenance"],
                    extra={"drift": snd.drift_note()},
                ).rstrip("\n"),
                "pressure_hpa,height_msl_m,temperature_c,dewpoint_c,u_ms,v_ms",
            ]
            keys = ("pressure_hpa", "height_msl_m", "temperature_c", "dewpoint_c", "u_ms", "v_ms")
            for row in zip(*(lv[k] for k in keys), strict=True):
                lines.append(",".join("" if x is None else f"{x:g}" for x in row))
            Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            raise UsageError(f"-o for sounding must end in .json or .csv, not {suffix or target!r}")
        out.wrote(target)
    if out.json:
        payload = dict(result)
        payload["levels"] = {k: v for k, v in result["levels"].items() if k in ("n_levels",)}
        payload["files"] = out.files
        out.emit_json(payload)
        return EXIT_OK
    head = " ".join(x for x in (snd.station, snd.launch_time or "launch time not in the file") if x)
    out.result(f"{head}  ({snd.fmt}, {result['levels']['n_levels']} levels, {Path(path).name})")
    out.result(f"data class: {result['provenance']['kind']}; {result['drift_note']}")
    out.result("")
    for line in sounding.summary_lines(diag):
        out.result(line)
    if diag["notes"]:
        out.result("")
        for note in diag["notes"]:
            out.result(f"note: {note}")
    if out.verbose:
        out.result("")
        out.result("definitions (heights are m above the launch site):")
        for key, text in diag["definitions"].items():
            out.result(f"  {key}: {text}")
        out.result("parcels:")
        for key, text in diag["parcel_definitions"].items():
            out.result(f"  {key}: {text}")
        out.result("references:")
        for text in diag["references"].values():
            out.result(f"  {text}")
        out.result(f"libraries: {provenance.libraries_string()}")
    return EXIT_OK


def cmd_plot(args: argparse.Namespace, out: Out) -> int:
    path = _inputs(args, maximum=1)[0]
    targets = [t for t in [args.plot, *args.output] if t]
    if len(targets) > 1:
        raise UsageError("plot writes one figure: give a single -p / -o file")
    with _open(path) as vol:
        if args.variable:
            sweep = _need_sweep(out, vol, [args.variable], args.sweep)
            variable = vol.resolve(args.variable, sweep)
        else:
            sweep = 0 if args.sweep is None else args.sweep
            variable = vol._default_variable(sweep)
            out.chose(
                f"variable {variable} (the first reflectivity field; -V NAME to change)",
                "-V",
                variable,
            )
        target = targets[0] if targets else None
        fmt = (args.format or "").lower().lstrip(".")
        if target is None:
            from engine.api_results import volume_stem

            name = f"{volume_stem(vol)}_{variable.replace(':', '_')}_sweep{sweep}"
            target = f"{name}.{fmt or 'png'}"
        elif not Path(target).suffix and fmt:
            target = f"{target}.{fmt}"
        if Path(target).suffix.lower() not in (".png", ".svg", ".pdf"):
            raise UsageError(f"figure file {target!r}: use a .png, .svg or .pdf name (or -f png)")
        fig = vol.plot(
            variable,
            sweep,
            dpi=args.dpi,
            theme=args.theme,
            vmin=args.vmin,
            vmax=args.vmax,
            title=args.title,
            rings=not args.no_rings,
        )
    _save_plot(fig, target, args.dpi, out)
    if out.json:
        out.emit_json({"file": path, "variable": variable, "sweep": sweep, "files": out.files})
    return EXIT_OK


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def cmd_export(args: argparse.Namespace, out: Out) -> int:
    path = _inputs(args, maximum=1)[0]
    if args.include_derived and not (args.pipeline or args.run):
        raise UsageError(
            "--include-derived needs derived variables: compute them in the same command with "
            '--pipeline RECIPE and/or --run MODULE (for example --pipeline "Reflectivity QC")'
        )
    if len(args.output) > 1:
        raise UsageError("export writes one file: give a single -o")
    fmt = (args.format or "").lower() or None
    target = args.output[0] if args.output else None
    if fmt is None and target is None:
        fmt = "netcdf"
        out.chose(
            "NetCDF (CfRadial1) as the format (add -o FILE.zarr / .csv / .png or -f to change)",
            "-f",
            "netcdf",
        )
    with _open(path) as vol:
        computed: list[str] = []
        for recipe in args.pipeline:
            computed += vol.pipeline(recipe, all_sweeps=True, progress=out.progress).derived
        for module in args.run:
            name, _, rest = module.partition(":")
            params = _split_kv([p for p in rest.split(",") if p], "--run")
            computed += vol.run(name, all_sweeps=True, params=params, progress=out.progress).derived
        out.progress_done()
        sweep = args.sweep
        if sweep is None:
            sweep = vol.default_sweep([args.variable]) if args.variable else 0
        written = vol.export(
            fmt,
            target,
            variable=args.variable,
            sweep=sweep,
            include_derived=computed if computed else False,
            dpi=args.dpi,
            progress=out.progress,
        )
    out.progress_done()
    out.wrote(written)
    if out.json:
        out.emit_json({"file": path, "derived": computed, "files": out.files})
    return EXIT_OK


# ---------------------------------------------------------------------------
# examples
# ---------------------------------------------------------------------------

_TOUR: dict[str, str] = {
    "look": f"""\
# LOOK - what is in the file, and a first picture
xrd info -i {SAMPLE}
xrd plot -i {SAMPLE}
xrd plot -i {SAMPLE} -V velocity -p velocity.png --theme dark
xrd info -i mayfield/KHPX mayfield/KNQA --json""",
    "clean": f"""\
# CLEAN - QC recipes (clutter, biological echo, dealiasing, speckle)
xrd pipeline list
xrd pipeline show "Doppler velocity"
xrd pipeline "Doppler velocity" -i {SAMPLE} --target VRADH -p before_after.png
xrd pipeline "Reflectivity QC" -i {SAMPLE} -P snr_gate.min_snr=5 -P gap_fill=off -o qc.nc""",
    "analyse": f"""\
# ANALYSE - algorithms, storm cells
xrd modules
xrd modules show radarx_hid
xrd run radarx_hid -i {SAMPLE} -p hid.png
xrd run radarx_hid -i {SAMPLE} --all-sweeps -o results
xrd run storm_detection -i {SAMPLE} -P threshold_dbz=40 -o cells.csv
xrd cells -i {SAMPLE} -p cells.png
xrd cells -i 'mayfield/KHPX/*_V06' -o cells.csv --tracks tracks.csv -p tracks.png
xrd sounding -i BMX_20220330_00Z.csv --wind-units kt""",
    "combine": """\
# COMBINE - several radars of one time (needs radarx)
xrd mosaic -i KHPX20211211_015930_V06 KNQA20211211_015611_V06 KPAH20211211_015935_V06 KVWX20211211_015522_V06
xrd mosaic -i 'mayfield/*/*_0159*_V06' -o mosaic.nc -p map.png
xrd winds -i KHPX20211211_015930_V06 KNQA20211211_015611_V06 --extent 80 -o winds.nc -p winds.png --level-km 3""",
    "save": f"""\
# SAVE - NetCDF, Zarr, CSV, images
xrd export -i {SAMPLE} -o ktlx.nc
xrd export -i {SAMPLE} -o ktlx.zarr
xrd export -i {SAMPLE} -o dbz.csv -V DBZH -s 0
xrd export -i {SAMPLE} -o qc.nc --pipeline "Reflectivity QC" --run radarx_hid --include-derived""",
    "reproduce": f"""\
# REPRODUCE - keep an analysis in a file, print the command that was run
xrd run radarx_hid -i {SAMPLE} --print-command
xrd --config run.toml
ls mayfield/KHPX/*_V06 | xrd info -i -""",
}
TOPICS = tuple(_TOUR)
#: ``xrd examples mosaic`` works too: a command name selects its topic.
TOPIC_ALIASES = {
    "info": "look",
    "plot": "look",
    "pipeline": "clean",
    "run": "analyse",
    "modules": "analyse",
    "cells": "analyse",
    "sounding": "analyse",
    "mosaic": "combine",
    "winds": "combine",
    "export": "save",
    "config": "reproduce",
}

CONFIG_EXAMPLE = f"""\
# run.toml - the same names as the long options; run it with:  xrd --config run.toml
command = "run"
module = "radarx_hid"
input = ["{SAMPLE}"]
all_sweeps = true
output = "results"

[param]
method = "park"
"""


def cmd_examples(args: argparse.Namespace, out: Out) -> int:
    topic = TOPIC_ALIASES.get(args.topic, args.topic) if args.topic else None
    if topic and topic not in _TOUR:
        close = difflib.get_close_matches(args.topic, [*TOPICS, *TOPIC_ALIASES], n=1)
        hint = f" Did you mean '{close[0]}'?" if close else ""
        raise UsageError(
            f"unknown topic '{args.topic}'.{hint} Topics: {', '.join(TOPICS)} "
            "(or a command name such as mosaic)"
        )
    parts = [_TOUR[topic]] if topic else list(_TOUR.values())
    text = "\n\n".join(parts)
    if not topic or topic == "reproduce":
        text += "\n\n# run.toml for 'xrd --config run.toml':\n" + "\n".join(
            ("#   " + ln).rstrip() for ln in CONFIG_EXAMPLE.splitlines()
        )
    out.result(text)
    if not topic:
        out.result("\nOne topic only: xrd examples " + "|".join(TOPICS))
    return EXIT_OK


def tour_commands() -> list[list[str]]:
    """The ``xrd ...`` lines of the guided tour as argv lists (used by the tests)."""
    found = []
    for text in _TOUR.values():
        for line in text.splitlines():
            if line.startswith("xrd "):
                found.append(shlex.split(line.split(" | ")[0])[1:])
    return found


# ---------------------------------------------------------------------------
# completion
# ---------------------------------------------------------------------------


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def completion_script(shell: str) -> str:
    """A tab-completion script for *shell* (``bash``, ``zsh`` or ``fish``)."""
    parser = build_parser()
    subs = _subparsers(parser)
    opts: dict[str, list[str]] = {}
    values: dict[str, list[str]] = {}
    descs: dict[str, str] = {}
    for name, sp in subs.items():
        flags: list[str] = []
        for action in sp._actions:
            if action.help == argparse.SUPPRESS:
                continue
            flags += [o for o in action.option_strings if o.startswith("-")]
            if action.choices and action.option_strings:
                for o in action.option_strings:
                    values[f"{name}:{o}"] = [str(c) for c in action.choices]
        opts[name] = sorted(set(flags))
        descs[name] = (sp.description or "").split(".")[0]
    modules, recipes = _completion_names()
    positional = {
        "run": modules,
        "modules": ["list", "show", "run", *modules],
        "pipeline": ["list", "show", "run", "script", *recipes],
        "examples": list(TOPICS),
        "completion": ["bash", "zsh", "fish"],
    }
    if shell == "fish":
        lines = [
            "# xrd completion for fish: xrd completion fish > ~/.config/fish/completions/xrd.fish"
        ]
        lines.append("complete -c xrd -f")
        for name in COMMANDS:
            d = descs.get(name, "").replace("'", "")
            lines.append(f"complete -c xrd -n __fish_use_subcommand -a {name} -d '{d}'")
        for name, flags in opts.items():
            for flag in flags:
                kind = "-s" if re.fullmatch(r"-[A-Za-z]", flag) else "-l"
                short = flag.lstrip("-")
                extra = ""
                if (f"{name}:{flag}") in values:
                    extra = f" -r -f -a '{' '.join(values[f'{name}:{flag}'])}'"
                elif flag in ("--input", "-i", "--output", "-o", "--plot", "-p", "--config"):
                    extra = " -r -F"
                lines.append(
                    f"complete -c xrd -n '__fish_seen_subcommand_from {name}' {kind} {short}{extra}"
                )
        for name, words in positional.items():
            lines.append(
                f"complete -c xrd -n '__fish_seen_subcommand_from {name}' -a '{' '.join(words)}'"
            )
        return "\n".join(lines) + "\n"
    case_opts = "\n".join(f'    {n}) opts="{" ".join(f)}" ;;' for n, f in opts.items())
    case_vals = "\n".join(
        f'    {k.split(":")[0]}:{k.split(":", 1)[1]}) words="{" ".join(v)}" ;;'
        for k, v in values.items()
    )
    case_pos = "\n".join(f'    {n}) words="{" ".join(w)}" ;;' for n, w in positional.items())
    script = f"""\
# xrd completion for bash/zsh. Install:
#   bash: xrd completion bash > ~/.local/share/bash-completion/completions/xrd
#   zsh:  source <(xrd completion zsh)      (needs: autoload -U compinit && compinit)
_xrd() {{
  local cur prev cmd opts words
  COMPREPLY=()
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD-1]}}"
  cmd="${{COMP_WORDS[1]}}"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "{" ".join(COMMANDS)} --help --version" -- "$cur") )
    return 0
  fi
  case "$cmd" in
{case_opts}
    *) opts="" ;;
  esac
  words=""
  case "$cmd:$prev" in
{case_vals}
  esac
  if [ -n "$words" ]; then
    COMPREPLY=( $(compgen -W "$words" -- "$cur") )
    return 0
  fi
  if [[ "$cur" == -* ]]; then
    COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
    return 0
  fi
  if [ "$COMP_CWORD" -eq 2 ]; then
    case "$cmd" in
{case_pos}
    esac
    COMPREPLY=( $(compgen -W "$words" -- "$cur") )
  fi
  COMPREPLY+=( $(compgen -f -- "$cur") )
}}
"""
    if shell == "zsh":
        return (
            script
            + "autoload -U +X bashcompinit && bashcompinit\ncomplete -o default -F _xrd xrd\n"
        )
    return script + "complete -o default -F _xrd xrd\n"


def _completion_names() -> tuple[list[str], list[str]]:
    """Module ids and recipe names for the completion script (best effort, never fails)."""
    try:
        from engine import api

        return [m["id"] for m in api.modules()], [shlex.quote(r["name"]) for r in api.recipes()]
    except Exception:
        return [], []


def cmd_completion(args: argparse.Namespace, out: Out) -> int:
    sys.stdout.write(completion_script(args.shell))
    return EXIT_OK


# ---------------------------------------------------------------------------
# configuration files and argv preparation
# ---------------------------------------------------------------------------


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def load_config(path: str) -> dict[str, Any]:
    """Read a ``.toml`` / ``.yaml`` / ``.json`` options file."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise UsageError(f"config file not found: {path}")
    suffix = p.suffix.lower()
    try:
        if suffix == ".toml":
            try:
                import tomllib
            except ImportError as exc:  # Python 3.10
                raise UsageError(
                    "reading .toml needs Python 3.11 or newer; use a .json or .yaml file instead"
                ) from exc
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        elif suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as exc:
                raise UsageError(
                    "reading .yaml needs PyYAML (pip install pyyaml); or use .toml / .json"
                ) from exc
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        elif suffix == ".json":
            data = json.loads(p.read_text(encoding="utf-8"))
        else:
            raise UsageError(f"config file {path}: use a .toml, .yaml or .json file")
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise UsageError(f"config file {path} must hold key = value pairs")
    return data


def config_tokens(cfg: dict[str, Any]) -> tuple[str | None, list[str], list[str]]:
    """``(command, positional words, option tokens)`` for a config mapping."""
    command = cfg.get("command")
    words: list[str] = []
    tokens: list[str] = []
    for key, val in cfg.items():
        if key == "command" or val is None:
            continue
        if key in ("module", "recipe", "action", "topic"):
            words.append(str(val))
            continue
        opt = "--" + ("param" if key == "params" else key.replace("_", "-"))
        if isinstance(val, bool):
            if val:
                tokens.append(opt)
        elif isinstance(val, dict):
            for k, v in val.items():
                tokens += [opt, f"{k}={_scalar(v)}"]
        elif isinstance(val, (list, tuple)):
            if key == "input":
                tokens += [opt, *map(str, val)]
            else:
                for v in val:
                    tokens += [opt, _scalar(v)]
        else:
            tokens += [opt, _scalar(val)]
    return (str(command) if command else None), words, tokens


def prepare_argv(argv: Sequence[str]) -> list[str]:
    """Apply the command aliases and a ``--config`` file to *argv*.

    ``modules run X`` -> ``run X``; ``modules list`` -> ``modules``; ``pipeline run R`` ->
    ``pipeline R``; ``tour`` -> ``examples``. The options of a config file are put right after the
    command, so anything typed on the command line wins.
    """
    args = list(argv)
    config = None
    for i, a in enumerate(args):
        if a == "--config" and i + 1 < len(args):
            config = args[i + 1]
            del args[i : i + 2]
            break
        if a.startswith("--config="):
            config = a.split("=", 1)[1]
            del args[i]
            break
    command, words, tokens = None, [], []
    if config:
        command, words, tokens = config_tokens(load_config(config))
    if not args or args[0].startswith("-"):
        if command:
            args.insert(0, command)
        elif config:
            raise UsageError(
                f"config file {config} has no 'command = ...' line: add one "
                '(for example command = "run")'
            )
    if args and args[0] == "tour":
        args[0] = "examples"
    if len(args) > 1 and args[0] == "modules" and args[1] == "run":
        args = ["run", *args[2:]]
    elif len(args) > 1 and args[0] == "modules" and args[1] == "list":
        args = ["modules", *args[2:]]
    if config and args and not args[0].startswith("-"):
        args[1:1] = [*words, *tokens]
    return args


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("xradar-desktop-engine")
    except Exception:
        return "0.1.0"


class _NoTraceback:
    """Log filter: print a warning's message but not its traceback (``-vv`` shows tracebacks)."""

    def filter(self, record: Any) -> bool:
        record.exc_info = record.exc_text = None
        return True


def _quiet_libraries(verbose: int) -> None:
    import logging

    if verbose >= 2:
        logging.basicConfig(level=logging.INFO if verbose == 2 else logging.DEBUG)
        return
    logging.disable(logging.INFO)
    warnings.simplefilter("ignore")  # library warnings (zarr, xarray ...) are noise; -vv shows them


#: Set by ``--no-color`` for the duration of one ``main()`` call (never leaks into the process).
_STATE = {"no_color": False}


def _long_options(parser: argparse.ArgumentParser, command: str) -> list[str]:
    sub = _subparsers(parser).get(command, parser)
    return [o for a in sub._actions for o in a.option_strings if o.startswith("--")]


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``xrd``. Returns the exit code (0 ok, 1 failed, 2 usage, 3 input, 4 package, 70 bug)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        argv = prepare_argv(argv)
        if not argv:
            print(overview())
            return EXIT_OK
        args, extras = parser.parse_known_args(argv)
        stray = [e for e in extras if e.startswith("-") and e != "-"]
        if stray:
            command = getattr(args, "command", "") or ""
            known = _long_options(parser, command)
            close = difflib.get_close_matches(stray[0].split("=")[0], known, n=1, cutoff=0.6)
            hint = f" Did you mean {close[0]}?" if close else ""
            raise UsageError(
                f"unrecognized option(s): {' '.join(stray)}.{hint}\n\n"
                f"Run '{PROG} {command} --help' for the options."
            )
    except UsageError as exc:
        print(f"{_err_label(sys.stderr)} {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as exc:  # --help
        return int(exc.code or 0)
    if args.version:
        print(f"{PROG} {_version()}")
        return EXIT_OK
    if not getattr(args, "func", None):
        print(overview())
        return EXIT_OK
    args.extra = list(extras)
    args.argv = list(argv)
    if args.command == "pipeline":  # bare words are recipe words (RECIPE, or list / script RECIPE)
        args.recipe_words, args.extra = args.extra, []
    if getattr(args, "debug", False):
        args.verbose = max(args.verbose, 2)
    import logging

    saved_logging = logging.root.manager.disable
    saved_color = _STATE["no_color"]
    _STATE["no_color"] = bool(getattr(args, "no_color", False))
    no_traceback = _NoTraceback()
    if args.verbose < 2:
        logging.lastResort.addFilter(
            no_traceback
        )  # a failed sweep warns in one line, not a traceback
    try:
        with warnings.catch_warnings():
            return _execute(args)
    finally:  # nothing of this run leaks into a process that calls main() repeatedly
        logging.lastResort.removeFilter(no_traceback)
        logging.disable(saved_logging)
        _STATE["no_color"] = saved_color


def _equivalent_command(args: argparse.Namespace, out: Out) -> str:
    """The full command that reproduces this run, with the choices made for the user spelled out."""
    tokens = [t for t in args.argv if t != "--print-command"]
    extra: list[str] = []
    for i in range(0, len(out.auto_args) - 1, 2):
        flag, value = out.auto_args[i], out.auto_args[i + 1]
        if flag not in args.argv:
            extra += [flag, value]
    return shlex.join([PROG, *tokens, *extra])


def _execute(args: argparse.Namespace) -> int:
    out = Out(args)
    _quiet_libraries(out.verbose)
    from engine import api  # lazy: keeps `xrd --help` instant

    user_errors = api.user_errors()
    debug = out.verbose >= 2
    started = time.perf_counter()

    def fail(exc: BaseException, code: int) -> int:
        if debug:
            raise exc
        print(f"{_err_label(sys.stderr)} {api.error_message(exc)}", file=sys.stderr)
        return code

    try:
        code = args.func(args, out)
    except UsageError as exc:
        print(f"{_err_label(sys.stderr)} {exc}", file=sys.stderr)
        code = EXIT_USAGE
    except api.XrdInputError as exc:
        code = fail(exc, EXIT_INPUT)
    except api.XrdDependencyError as exc:
        code = fail(exc, EXIT_DEPENDENCY)
    except api.XrdArgumentError as exc:
        code = fail(exc, EXIT_USAGE)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        code = EXIT_INTERRUPTED
    except user_errors as exc:
        needs_package = isinstance(exc, ImportError) or "pip install" in api.error_message(exc)
        code = fail(exc, EXIT_DEPENDENCY if needs_package else EXIT_FAILED)
    except Exception as exc:
        if debug:
            raise
        print(
            f"{_err_label(sys.stderr)} unexpected {type(exc).__name__}: {exc}\n"
            "This looks like a bug. Re-run with -vv for the traceback and report it.",
            file=sys.stderr,
        )
        code = EXIT_INTERNAL
    if code == EXIT_OK and getattr(args, "print_command", False):
        command = _equivalent_command(args, out)
        if out.json:
            print(f"# {command}", file=sys.stderr)
        else:
            print(f"command: {command}")
    if out.verbose and not out.json:
        print(f"[{time.perf_counter() - started:.1f} s]", file=sys.stderr)
    return code


def help_examples(parser: argparse.ArgumentParser | None = None) -> list[tuple[str, list[str]]]:
    """Every example line of every ``--help`` as ``(command name, argv)`` (used by the tests)."""
    parser = parser or build_parser()
    found: list[tuple[str, list[str]]] = []
    texts = {PROG: parser.epilog or ""}
    for name, sp in _subparsers(parser).items():
        texts[name] = sp.epilog or ""
    for name, text in texts.items():
        joined = text.replace("\\\n", " ")
        for line in joined.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{PROG} "):
                found.append((name, shlex.split(stripped)[1:]))
    return found


# keep the type alias importable for tests that stub the engine
CommandFn = Callable[[argparse.Namespace, Out], int]

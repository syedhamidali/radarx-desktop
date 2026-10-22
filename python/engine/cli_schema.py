"""Command Studio: every ``rxd`` command as a form in the app.

Two pure functions, used by the ``cli_describe`` / ``cli_run`` WebSocket messages of the server
and by the tests:

* :func:`describe` turns the real ``rxd`` argument parser (``engine.cli.build_parser``) into a
  machine-readable description of every sub-command: name, group, purpose, examples, and each
  option with its kind (flag, number, choice, input files, output path ...), choices, default,
  help, unit, whether it is repeatable, required or rarely used (advanced). Nothing is written by
  hand twice: a new option in the parser shows up in the app by itself.
* :func:`run_command` runs one command line inside the engine process, with the same entry points
  as the terminal (``args.func(args, out)``), the output captured, the files it wrote listed, the
  exit code mapped to a friendly sentence, progress forwarded to a callback and a cooperative
  cancel. It never raises for a failed command and never returns a traceback.

The account gate is not applied here: the server only answers ``cli_run`` on a connection whose
account is signed in, and the engine functions keep their own ``require_account()`` checks.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import difflib
import io
import os
import shlex
import time
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

#: Commands that make no sense as a form (the reason is shown instead of the Run button).
NOT_INTERACTIVE = {
    "login": "The app signs you in: use the account dialog (the terminal shares the account).",
    "logout": "Sign out from the account dialog; here it would also end the app's session.",
    "serve": "This engine is already serving the app: start 'rxd serve' in a terminal on the computer "
    "that holds the data.",
}

#: Option groups and options that are folded away under "Advanced" in the form.
ADVANCED_GROUPS = {"output control", "reproducibility"}
ADVANCED_DESTS = {
    "dpi", "no_color", "debug", "json", "quiet", "verbose", "config", "print_command", "api",
    "plot_sweep", "timeout", "backend", "format", "x264_preset", "crf", "fresh", "offline",
    "no_virtual_temperature", "basemap", "credit", "subtitle",
}  # fmt: skip

#: Units read off the option name (``--height-m`` -> m, ``--threshold-dbz`` -> dBZ ...).
UNIT_SUFFIXES = (
    ("_km2", "km²"), ("_km", "km"), ("_m", "m"), ("_s", "s"), ("_dbz", "dBZ"),
    ("_minutes", "min"), ("_deg", "°"), ("_mps", "m/s"),
)  # fmt: skip

#: Options whose value is a file the user picks (not a radar input, not an output).
FILE_METAVARS = {"FILE", "CSV", "JSON"}
OUTPUT_DESTS = {"output", "plot", "tracks"}

EXIT_TEXT = {
    0: "Done.",
    1: "The processing failed: the command could not run on this data.",
    2: "The command was used wrongly: check the options.",
    3: "An input file is missing or cannot be read.",
    4: "An optional Python package is missing; the message says how to install it.",
    5: "No signed-in account.",
    70: "An unexpected internal error; the details are in the messages.",
    130: "Cancelled.",
}

STATUS = {0: "ok", 130: "cancelled"}

MAX_THUMBNAIL_BYTES = 4 * 1024 * 1024
MAX_TEXT = 200_000


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


def _unit(dest: str) -> str | None:
    if dest == "dpi":
        return "dpi"
    if dest == "fps":
        return "fps"
    for suffix, unit in UNIT_SUFFIXES:
        if dest.endswith(suffix):
            return unit
    return None


def _kind(action: argparse.Action) -> str:
    """The control the form shows for *action*."""
    name = type(action).__name__
    if name == "_CountAction":
        return "count"
    if name in ("_StoreTrueAction", "_StoreFalseAction") or action.nargs == 0:
        return "flag"
    if action.dest == "input":
        return "input"
    if action.dest in OUTPUT_DESTS or (action.metavar == "PATH" and not action.option_strings):
        return "output"
    if action.choices:
        return "choice"
    if action.type is int:
        return "int"
    if action.type is float:
        return "float"
    if action.metavar in FILE_METAVARS and action.dest not in ("param",):
        return "file"
    if action.dest == "param":
        return "params"
    return "text"


def _repeatable(action: argparse.Action) -> bool:
    name = type(action).__name__
    return name in ("_AppendAction", "_ExtendAction") or action.nargs in ("+", "*")


def _default(action: argparse.Action) -> Any:
    d = action.default
    if d is argparse.SUPPRESS or d is None:
        return None
    if isinstance(d, (list, tuple)) and not d:
        return None
    return d


def _examples(epilog: str | None, prog: str) -> list[str]:
    """Every ``rxd ...`` example line of a help epilog (continuation lines joined)."""
    if not epilog:
        return []
    found: list[str] = []
    for line in epilog.replace("\\\n", " ").splitlines():
        stripped = " ".join(line.split())
        if stripped.startswith(f"{prog} "):
            found.append(stripped)
    return found


def _group_of(name: str, groups: Sequence[tuple[str, Sequence[Any]]]) -> tuple[str, str]:
    for title, items in groups:
        for cmd, purpose in items:
            if cmd == name:
                return title, purpose
    return "Other", ""


def _describe_command(name: str, sp: argparse.ArgumentParser, cli: Any) -> dict[str, Any]:
    exclusive: dict[str, list[str]] = {}
    for grp in sp._mutually_exclusive_groups:
        dests = [a.dest for a in grp._group_actions]
        for d in dests:
            exclusive[d] = [o for o in dests if o != d]
    options: list[dict[str, Any]] = []
    for action in sp._actions:
        if isinstance(action, argparse._HelpAction) or action.help == argparse.SUPPRESS:
            continue
        kind = _kind(action)
        group = str(getattr(action.container, "title", "") or "")
        if group in ("options", "optional arguments", "positional arguments"):
            group = ""
        advanced = group in ADVANCED_GROUPS or action.dest in ADVANCED_DESTS
        options.append(
            {
                "dest": action.dest,
                "flags": list(action.option_strings),
                "positional": not action.option_strings,
                "metavar": action.metavar if isinstance(action.metavar, str) else None,
                "kind": kind,
                "choices": [str(c) for c in action.choices] if action.choices else None,
                "default": _default(action),
                "help": " ".join(str(action.help or "").split()),
                "required": bool(action.required)
                or (not action.option_strings and action.nargs not in ("?", "*")),
                "repeatable": _repeatable(action),
                "nargs": action.nargs if isinstance(action.nargs, (int, str)) else None,
                "group": group,
                "advanced": advanced,
                "unit": _unit(action.dest),
                "exclusive": exclusive.get(action.dest, []),
            }
        )
    title, purpose = _group_of(name, cli.GROUPS)
    return {
        "name": name,
        "group": title,
        "purpose": purpose,
        "summary": " ".join(str(sp.description or "").split()),
        "epilog": str(sp.epilog or ""),
        "examples": _examples(sp.epilog, cli.PROG),
        "options": options,
        "interactive": name not in NOT_INTERACTIVE,
        "reason": NOT_INTERACTIVE.get(name),
    }


def describe(parser: argparse.ArgumentParser | None = None) -> dict[str, Any]:
    """The machine-readable description of every ``rxd`` command (see the module docstring)."""
    from engine import cli

    parser = parser or cli.build_parser()
    subs = cli._subparsers(parser)
    order = [name for _, items in cli.GROUPS for name, _ in items]
    names = [n for n in order if n in subs] + sorted(n for n in subs if n not in order)
    return {
        "prog": cli.PROG,
        "version": cli._version(),
        "groups": [title for title, _ in cli.GROUPS],
        "commands": [_describe_command(n, subs[n], cli) for n in names],
        "exit_codes": {str(k): v for k, v in EXIT_TEXT.items()},
    }


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------


class StudioCancelledError(Exception):
    """Raised inside the worker at the next progress tick after a cancel."""


class _StudioOut:
    """An ``Out`` whose progress always reaches the app, and that stops when cancelled."""

    def __new__(cls, args: argparse.Namespace, progress: Any, is_cancelled: Any) -> Any:
        from engine import cli

        class Out(cli.Out):
            def progress(self, percent: int, message: str) -> None:  # type: ignore[override]
                if is_cancelled is not None and is_cancelled():
                    raise StudioCancelledError()
                if progress is not None:
                    progress(max(0, min(99, int(percent))), str(message))

            def progress_done(self) -> None:  # type: ignore[override]
                self._last_pct = -1

        return Out(args)


def _file_record(path: str, cwd: str | None) -> dict[str, Any]:
    p = Path(path)
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    suffix = p.suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".svg", ".pdf"):
        kind = "image"
    elif suffix in (".nc", ".zarr", ".h5", ".hdf5", ".nc4"):
        kind = "radar"
    elif suffix in (".csv", ".json", ".txt", ".toml"):
        kind = "table"
    elif suffix in (".mp4", ".gif", ".webm"):
        kind = "video"
    else:
        kind = "other"
    rec: dict[str, Any] = {"path": str(p), "name": p.name, "kind": kind, "size": None}
    try:
        size = p.stat().st_size if p.is_file() else None
        rec["size"] = size
        if suffix in (".png", ".jpg", ".jpeg") and size is not None and size <= MAX_THUMBNAIL_BYTES:
            mime = "image/png" if suffix == ".png" else "image/jpeg"
            rec["thumbnail"] = f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    except OSError:
        pass
    return rec


def _clip(text: str) -> str:
    text = text.replace("\r", "\n")
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + "\n... (output shortened)"
    return text


def refused(message: str) -> dict[str, Any]:
    """A ``cli_result`` payload for a command that was not run at all."""
    return {
        "code": 2,
        "status": "failed",
        "ok": False,
        "message": message,
        "stdout": "",
        "stderr": "",
        "files": [],
        "command": None,
        "elapsed_ms": 0,
    }


def run_command(
    argv: Sequence[str],
    *,
    progress: Callable[[int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Run one ``rxd`` command line in this process and describe what happened.

    Returns ``{code, status, ok, message, stdout, stderr, files, command, elapsed_ms}``. The
    text streams are captured (so this must not run alongside another capture; the server
    serialises it), relative output names are resolved in *cwd* (the engine's working folder is
    switched for the duration), ``-`` (a list on stdin) is refused, and ``--help`` / ``--version``
    give their text with code 0.
    """
    from engine import api, cli

    argv = [str(a) for a in argv]
    started = time.perf_counter()
    buf_out, buf_err = io.StringIO(), io.StringIO()
    files: list[str] = []
    command: str | None = None
    code = cli.EXIT_INTERNAL
    message = ""
    saved_cwd = os.getcwd()
    use_cwd = cwd if cwd and os.path.isdir(cwd) else None

    def finish() -> dict[str, Any]:
        status = STATUS.get(code, "failed")
        text = message or EXIT_TEXT.get(code, EXIT_TEXT[70])
        return {
            "code": code,
            "status": status,
            "ok": code == 0,
            "message": text,
            "stdout": _clip(buf_out.getvalue()),
            "stderr": _clip(buf_err.getvalue()),
            "files": [_file_record(f, use_cwd or saved_cwd) for f in files],
            "command": command,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    if not argv:
        buf_out.write(cli.overview())
        code = 0
        return finish()
    if "-" in argv:
        code = cli.EXIT_USAGE
        message = "A file list on standard input (-) is not available here: pick the files instead."
        return finish()

    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        parser = cli.build_parser()
        try:
            prepared = cli.prepare_argv(argv)
            args, extras = parser.parse_known_args(prepared)
            stray = [e for e in extras if e.startswith("-") and e != "-"]
            if stray:
                known = cli._long_options(parser, getattr(args, "command", "") or "")
                close = difflib.get_close_matches(stray[0].split("=")[0], known, n=1, cutoff=0.6)
                hint = f" Did you mean {close[0]}?" if close else ""
                raise cli.UsageError(f"unrecognized option(s): {' '.join(stray)}.{hint}")
        except cli.UsageError as exc:
            code, message = cli.EXIT_USAGE, str(exc).split("\n\nRun '")[0]
            return finish()
        except SystemExit as exc:  # --help
            code = int(exc.code or 0)
            message = "Help printed." if code == 0 else "The command was used wrongly."
            return finish()
        if getattr(args, "version", False):
            print(f"{cli.PROG} {cli._version()}")
            code = 0
            return finish()
        if not getattr(args, "func", None):
            print(cli.overview())
            code = 0
            return finish()
        if args.command in NOT_INTERACTIVE:
            code, message = cli.EXIT_USAGE, NOT_INTERACTIVE[args.command]
            return finish()
        args.extra = list(extras)
        args.argv = list(prepared)
        if args.command == "pipeline":
            args.recipe_words, args.extra = args.extra, []
        if getattr(args, "debug", False):
            args.verbose = max(args.verbose, 2)
        out = _StudioOut(args, progress, is_cancelled)
        user_errors = api.user_errors()
        if use_cwd:
            os.chdir(use_cwd)
        try:
            with warnings.catch_warnings():
                cli._quiet_libraries(out.verbose)
                try:
                    code = int(args.func(args, out))
                except cli.UsageError as exc:
                    code, message = cli.EXIT_USAGE, str(exc)
                except StudioCancelledError:
                    code = cli.EXIT_INTERRUPTED
                except api.XrdInputError as exc:
                    code, message = cli.EXIT_INPUT, api.error_message(exc)
                except api.XrdDependencyError as exc:
                    code, message = cli.EXIT_DEPENDENCY, api.error_message(exc)
                except api.XrdArgumentError as exc:
                    code, message = cli.EXIT_USAGE, api.error_message(exc)
                except KeyboardInterrupt:
                    code = cli.EXIT_INTERRUPTED
                except user_errors as exc:
                    text = api.error_message(exc)
                    needs_package = isinstance(exc, ImportError) or "pip install" in text
                    code = cli.EXIT_DEPENDENCY if needs_package else cli.EXIT_FAILED
                    message = text
                except Exception as exc:
                    code = cli.EXIT_INTERNAL
                    message = (
                        f"Unexpected {type(exc).__name__}: {' '.join(str(exc).split())[:300]}. "
                        "This looks like a bug; please report it."
                    )
            files = list(out.files)
            if code == 0:
                with contextlib.suppress(Exception):
                    command = cli._equivalent_command(args, out)
            if code == 0 and getattr(out, "auto", None):
                message = "Done. " + " ".join(f"Using {t}." for t in out.auto)
        finally:
            with contextlib.suppress(OSError):
                os.chdir(saved_cwd)
    return finish()


# ---------------------------------------------------------------------------
# reveal (Show in folder)
# ---------------------------------------------------------------------------


def shell_line(argv: Sequence[str]) -> str:
    """The command line as it would be typed (``rxd`` first)."""
    return shlex.join(["rxd", *[str(a) for a in argv]])

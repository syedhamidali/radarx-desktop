"""Movie Studio: the engine behind the app's movie dialog (the WebSocket form of ``rxd animate``).

A mixin for ``server.ConnectionHandler`` (it uses ``self.send``, ``self.send_progress`` and
``self.session``), in its own file so the server only gains three lines. Every message calls the
same functions as the command line (:mod:`engine.animate`); nothing is spawned in a shell.

Messages (``request_id`` is echoed on every answer):

* ``animate_capabilities {}`` -> ``animate_capabilities {ready, problems: [{what, hint}], sound,
  renderer, chrome, av, playwright, radarx, default_output_dir, cache_dir, choices}``:
  what rendering needs, checked up front, each with a fix hint in plain words.
* ``animate_plan {inputs: [paths], options: {...}}`` -> progress, then
  ``animate_plan_result {plan, report}`` (``rxd animate --no-render``: steps, track, frames, camera)
  or ``animate_error {message}``.
* ``animate_still {inputs, options, still_at, width, height}`` -> progress, then
  ``animate_still_result {png (base64), still_at_s, plan}`` (``rxd animate -p --still-at``).
* ``animate_render {inputs, options, output}`` -> progress, then ``animate_render_result {plan,
  video, provenance, output}``; ``animate_cancel {request_id}`` -> ``animate_cancelled``.
* ``animate_video {path}`` -> ``animate_video {byte_length, chunks}`` + binary frames (the MP4
  that was just written, so the app can play it), only for a file this engine wrote.
* ``animate_copy {src, dst}`` -> ``animate_copied {path, sidecar}``: "Export a copy".

``options`` holds the long option names of ``rxd animate`` (``camera``, ``crop_km``, ``realistic``,
``light``, ``sound`` ...); unknown names are refused with the nearest one.
"""

# ruff: noqa: E501
from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import difflib
import logging
import os
import re
import shlex
import shutil
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_PENDING = 8
CAMERAS = ["orbit", "wide", "top-down", "window", "flight"]
PALETTES = ["white", "soft", "stormy", "cyclone"]
LIGHTS = ["daylight", "dusk", "overcast", "night"]
TRACKS = ["cells", "linear", "eye"]

#: Options the app may set (the long names of ``rxd animate``) and their types.
_OPTION_TYPES: dict[str, type | tuple[type, ...]] = {
    "reference": str,
    "tolerance_s": (int, float),
    "min_radars": int,
    "min_step_s": (int, float),
    "start": str,
    "end": str,
    "max_steps": int,
    "detail_m": (int, float),
    "crop_km": (int, float),
    "cell_dbz": (int, float),
    "track": str,
    "winds": bool,
    "fresh": bool,
    "duration": (int, float),
    "timelapse": (int, float),
    "fps": int,
    "width": int,
    "height": int,
    "camera": str,
    "target_lag": (int, float),
    "fov": (int, float),
    "view_km": (int, float),
    "vexag": (int, float),
    "look_preset": str,
    "set_values": list,
    "realistic": bool,
    "ground": str,
    "storm_style": str,
    "palette": str,
    "light": str,
    "map_theme": str,
    "basemap": str,
    "title": str,
    "subtitle": str,
    "credit": str,
    "lightning_model": bool,
    "sea": bool,
    "offline": bool,
    "software": bool,
    "crf": int,
    "frame_format": str,
    "keep_frames": bool,
    # sound (only when the engine has it; see capabilities)
    "sound": bool,
    "sound_thunder_db": (int, float),
    "sound_seed": int,
    "sound_compress": (int, float),
    "flashes": str,
    "flashes_from_glm": list,
    "glm_radius_km": (int, float),
    "glm_bin_s": (int, float),
    "draw_flashes": bool,
}


class MovieRequestError(ValueError):
    """A request the app made that cannot be served as asked (the text says what to change)."""


# ---------------------------------------------------------------------------
# What rendering needs
# ---------------------------------------------------------------------------


def find_chrome() -> dict[str, Any]:
    """Where a browser for the renderer is: the system Chrome, else Playwright's own Chromium."""
    candidates: list[Path] = []
    if sys.platform == "darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            candidates += [
                base / "Google Chrome.app/Contents/MacOS/Google Chrome",
                base / "Chromium.app/Contents/MacOS/Chromium",
            ]
    elif sys.platform.startswith("win"):
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if root:
                candidates.append(Path(root) / "Google/Chrome/Application/chrome.exe")
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    for c in candidates:
        if c.is_file():
            return {"ok": True, "path": str(c), "channel": "chrome"}
    pw = Path.home() / ".cache" / "ms-playwright"
    if pw.is_dir() and any(p.name.startswith("chromium") for p in pw.iterdir()):
        return {"ok": True, "path": str(pw), "channel": ""}
    return {
        "ok": False,
        "channel": "chrome",
        "hint": "Google Chrome was not found. Install it from google.com/chrome (nothing is downloaded by the app), "
        "or run `python -m playwright install chromium` to use Playwright's own browser.",
    }


def capabilities() -> dict[str, Any]:
    """Everything the Movie Studio needs, checked up front, with a fix hint for each missing piece."""
    from engine import animate_render as render
    from engine.exporter import default_export_dir

    status = render.video_status()
    problems: list[dict[str, str]] = []
    radarx = render.have_module("radarx")
    if not radarx:
        problems.append(
            {
                "what": "The radarx package is missing (it merges the radars onto the 3D grid).",
                "hint": "Install it with: pip install -U radarx   (Python 3.12 or newer)",
            }
        )
    frozen = bool(getattr(sys, "frozen", False))
    if not status["playwright"]:
        problems.append(
            {
                "what": "Playwright is missing (it drives Chrome, which draws the frames).",
                "hint": (
                    "The movie components are not part of this installer. They will come as an "
                    "optional download in a later update; until then movies can be made with the "
                    "Python package (see Installation in the documentation)."
                    if frozen
                    else f"Install the video extra: {render.INSTALL_VIDEO}"
                ),
            }
        )
    chrome = find_chrome()
    if not chrome["ok"]:
        problems.append({"what": "No browser for the renderer.", "hint": chrome["hint"]})
    renderer: dict[str, Any]
    try:
        renderer = {"ok": True, "path": str(render.find_frontend(None))}
    except Exception as exc:  # XrdDependencyError with the fix in its text
        text = str(exc)
        renderer = {"ok": False, "hint": text}
        problems.append(
            {
                "what": "The 3D renderer files of the app were not found (the frames are drawn with them).",
                "hint": text.split("\n", 1)[1].strip() if "\n" in text else text,
            }
        )
    av_note = None
    if not status["av"]:
        av_note = (
            f"PyAV is missing, so no MP4 can be written: the frames would be saved as PNG files. "
            f"Install the video extra: {render.INSTALL_VIDEO}"
        )
        problems.append({"what": "PyAV is missing (it writes the MP4).", "hint": av_note})
    try:
        out_dir = str(default_export_dir())
    except ValueError:
        out_dir = ""
    from engine import animate_data as data

    return {
        "type": "animate_capabilities",
        "ready": not problems,
        "plan_ready": radarx,
        "problems": problems,
        "radarx": radarx,
        "playwright": status["playwright"],
        "av": status["av"],
        "chrome": chrome,
        "renderer": renderer,
        "sound": render.have_module("engine.animate_sound") and status["av"],
        "default_output_dir": out_dir,
        "cache_dir": str(data.default_cache_dir_for(None)),
        "choices": {"cameras": CAMERAS, "palettes": PALETTES, "lights": LIGHTS, "tracks": TRACKS},
    }


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def options_from_message(
    inputs: Any,
    raw: Any,
    *,
    output: str | None = None,
    still: str | None = None,
    still_at: float | None = None,
    no_render: bool = False,
) -> Any:
    """An :class:`engine.animate.AnimateOptions` from the app's request (checked, with hints)."""
    from engine import animate
    from engine.animate_cli import expand_animate_inputs

    if (
        not isinstance(inputs, list)
        or not inputs
        or not all(isinstance(p, str) and p for p in inputs)
    ):
        raise MovieRequestError(
            "choose the radar files to film first (a folder, or files already open)"
        )
    files = expand_animate_inputs(inputs)
    if not files:
        raise MovieRequestError("no radar files were found in what you chose")
    raw = raw or {}
    if not isinstance(raw, dict):
        raise MovieRequestError("'options' must be an object")
    values: dict[str, Any] = {}
    for key, val in raw.items():
        if key not in _OPTION_TYPES:
            close = difflib.get_close_matches(str(key), list(_OPTION_TYPES), n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            raise MovieRequestError(f"unknown movie option {key!r}{hint}")
        if val is None or val == "":
            continue
        want = _OPTION_TYPES[key]
        if want is bool:
            values[key] = bool(val)
        elif want is int:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise MovieRequestError(f"movie option {key!r} must be a number")
            values[key] = int(val)
        elif want is list:
            if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
                raise MovieRequestError(f"movie option {key!r} must be a list of strings")
            values[key] = list(val)
        elif want is str:
            values[key] = str(val)
        else:
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise MovieRequestError(f"movie option {key!r} must be a number")
            values[key] = float(val)
    if values.get("reference", "").lower() in ("", "auto"):
        values.pop("reference", None)
    else:
        values["reference"] = values["reference"].upper()
    if (
        values.get("camera")
        and values["camera"] not in CAMERAS
        and not values["camera"].endswith((".toml", ".json"))
    ):
        raise MovieRequestError(
            f"unknown camera {values['camera']!r}: choose one of {', '.join(CAMERAS)}"
        )
    chrome = find_chrome()
    opts = animate.AnimateOptions(
        files=files,
        output=output,
        still=still,
        still_at=still_at,
        no_render=no_render,
        browser=chrome.get("channel", "chrome"),
        **values,
    )
    opts.command = equivalent_command(
        inputs, values, output=output, still=still, still_at=still_at, no_render=no_render
    )
    return opts


def equivalent_command(
    inputs: list[str],
    values: dict[str, Any],
    *,
    output: str | None,
    still: str | None,
    still_at: float | None,
    no_render: bool,
) -> str:
    """The ``rxd animate`` line that does the same, recorded in the film's provenance file."""
    args: list[str] = ["rxd", "animate"]
    for p in inputs:
        args += ["-i", p]
    if output:
        args += ["-o", output]
    if still:
        args += ["-p", still]
    if still_at is not None:
        args += ["--still-at", f"{still_at:g}"]
    if no_render:
        args.append("--no-render")
    renamed = {"look_preset": "look-preset", "set_values": "set", "frame_format": "frame-format"}
    for key, val in values.items():
        flag = "--" + renamed.get(key, key).replace("_", "-")
        if key in ("width", "height"):
            continue
        if isinstance(val, bool):
            if val:
                args.append(flag)
        elif isinstance(val, list):
            for v in val:
                args += [flag, str(v)]
        else:
            args += [flag, f"{val:g}" if isinstance(val, float) else str(val)]
    if "width" in values or "height" in values:
        args += ["--size", f"{values.get('width', 1920)}x{values.get('height', 1080)}"]
    return shlex.join(args)


# ---------------------------------------------------------------------------
# Running with progress and cancel
# ---------------------------------------------------------------------------

_FRAME_RE = re.compile(r"^frame (\d+)/(\d+)")


class _Reporter:
    """Turns the ``note`` / ``detail`` lines of :func:`engine.animate.run` into progress percentages,
    and stops the run when the app cancelled it."""

    def __init__(
        self, progress: Callable[[int, str], None], cancel: threading.Event, rendering: bool
    ):
        self.progress = progress
        self.cancel = cancel
        self.rendering = rendering
        self.percent = 2
        self.notes: list[str] = []

    def _check(self) -> None:
        if self.cancel.is_set():
            from engine.animate import AnimateCancelledError

            raise AnimateCancelledError("cancelled")

    def note(self, text: str) -> None:
        self._check()
        self.notes.append(text)
        m = _FRAME_RE.match(text)
        if m:
            k, total = int(m[1]), int(m[2])
            self.percent = 20 + int(72 * k / max(1, total))
        elif text.startswith("encoding"):
            self.percent = 94
        elif "time steps from" in text:
            self.percent = 6
        self.progress(self.percent, text)

    def detail(self, text: str) -> None:
        self._check()
        if "grid" in text or "step" in text:
            self.percent = min(19 if self.rendering else 90, self.percent + 1)
            self.progress(self.percent, text)

    def frame(self, k: int, total: int) -> bool:
        if self.cancel.is_set():
            return True
        self.percent = 20 + int(72 * k / max(1, total))
        if k % max(1, total // 100) == 0 or k == total:
            self.progress(self.percent, f"frame {k} of {total}")
        return False


def _friendly(exc: BaseException) -> str:
    """The error as a sentence for the dialog (never a traceback)."""
    text = " ".join(str(exc).split())
    if not text:
        text = type(exc).__name__
    return text[:1200]


def _plan_payload(res: Any) -> dict[str, Any]:
    from engine import animate, cli

    doc = res.to_dict()
    doc["report"] = animate.format_report(res, cli._table)
    doc["command"] = res.options.command
    return doc


class _LoopLock:
    """One asyncio lock per running loop (the server's own pattern): one film at a time."""

    def __init__(self) -> None:
        self._entry: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None

    def get(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._entry is None or self._entry[0] is not loop:
            self._entry = (loop, asyncio.Lock())
        return self._entry[1]


_LOCK = _LoopLock()
_RUNS: dict[str, threading.Event] = {}
_WRITTEN: set[str] = set()  # files this engine wrote (the only ones animate_video serves)


def _cancel_event(request_id: str | None) -> threading.Event:
    if request_id is None:
        return threading.Event()
    ev = _RUNS.get(request_id)
    if ev is None:
        while len(_RUNS) >= MAX_PENDING:
            _RUNS.pop(next(iter(_RUNS)))
        ev = _RUNS[request_id] = threading.Event()
    return ev


async def _run_locked(func: Callable[[], Any]) -> Any:
    """Run ``func`` in a worker thread while holding the movie lock until the thread really ends."""
    loop = asyncio.get_running_loop()
    lock = _LOCK.get()
    await lock.acquire()

    def job() -> Any:
        try:
            return func()
        finally:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(lock.release)

    try:
        future = loop.run_in_executor(None, job)
    except BaseException:
        lock.release()
        raise
    return await future


class AnimateMessages:
    """Mixin for ``ConnectionHandler``: the Movie Studio messages."""

    def _animate_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[None]]]:
        return {
            "animate_capabilities": self._handle_animate_capabilities,
            "animate_plan": self._handle_animate_plan,
            "animate_still": self._handle_animate_still,
            "animate_render": self._handle_animate_render,
            "animate_cancel": self._handle_animate_cancel,
            "animate_video": self._handle_animate_video,
            "animate_copy": self._handle_animate_copy,
        }

    # -- helpers ------------------------------------------------------------------------------

    @staticmethod
    def _animate_id(msg: dict[str, Any]) -> str | None:
        rid = msg.get("request_id")
        return rid if isinstance(rid, str) and 0 < len(rid) <= 80 else None

    async def _animate_refuse_web(self, msg: dict[str, Any]) -> bool:
        if getattr(self, "session", None) is None:
            return False
        await self.send(  # type: ignore[attr-defined]
            {
                "type": "animate_error",
                "request_id": msg.get("request_id"),
                "message": "The Movie Studio renders on this computer with Chrome: it is available in the desktop app only.",
            }
        )
        return True

    async def _animate_error(self, request_id: Any, exc: BaseException) -> None:
        from engine.animate_plan import AnimateConfigError, AnimatePlanError
        from engine.api_results import XrdError

        if not isinstance(
            exc,
            (
                AnimateConfigError,
                AnimatePlanError,
                MovieRequestError,
                XrdError,
                OSError,
                ValueError,
            ),
        ):
            logger.error("movie failed: %s", exc, exc_info=exc)
        await self.send(  # type: ignore[attr-defined]
            {"type": "animate_error", "request_id": request_id, "message": _friendly(exc)}
        )

    def _animate_progress(
        self, loop: asyncio.AbstractEventLoop, request_id: Any
    ) -> Callable[[int, str], None]:
        def report(percent: int, text: str) -> None:
            if loop.is_closed():
                return
            asyncio.run_coroutine_threadsafe(
                self.send(  # type: ignore[attr-defined]
                    {
                        "type": "animate_progress",
                        "request_id": request_id,
                        "percent": percent,
                        "message": text,
                    }
                ),
                loop,
            )

        return report

    async def _animate_run(
        self, msg: dict[str, Any], build: Callable[[], Any], rendering: bool
    ) -> Any:
        """Build the options, run ``engine.animate.run`` in a worker with progress and cancel."""
        from engine import animate

        request_id = msg.get("request_id")
        key = self._animate_id(msg)
        cancel = _cancel_event(key)
        loop = asyncio.get_running_loop()
        reporter = _Reporter(self._animate_progress(loop, request_id), cancel, rendering)
        opts = build()

        def work() -> Any:
            if cancel.is_set():
                raise animate.AnimateCancelledError("cancelled")
            return animate.run(
                opts, note=reporter.note, detail=reporter.detail, on_frame=reporter.frame
            )

        try:
            return await _run_locked(work), opts
        except BaseException:
            cancel.set()  # the connection went away: the worker stops at its next frame
            raise
        finally:
            if key is not None:
                _RUNS.pop(key, None)

    # -- handlers -----------------------------------------------------------------------------

    async def _handle_animate_capabilities(self, msg: dict[str, Any]) -> None:
        if await self._animate_refuse_web(msg):
            return
        loop = asyncio.get_running_loop()
        caps = await loop.run_in_executor(None, capabilities)
        await self.send({**caps, "request_id": msg.get("request_id")})  # type: ignore[attr-defined]

    async def _handle_animate_plan(self, msg: dict[str, Any]) -> None:
        if await self._animate_refuse_web(msg):
            return
        request_id = msg.get("request_id")
        from engine.animate import AnimateCancelledError

        try:
            res, _opts = await self._animate_run(
                msg,
                lambda: options_from_message(msg.get("inputs"), msg.get("options"), no_render=True),
                rendering=False,
            )
            await self.send(  # type: ignore[attr-defined]
                {
                    "type": "animate_plan_result",
                    "request_id": request_id,
                    "plan": _plan_payload(res),
                }
            )
        except AnimateCancelledError:
            await self.send({"type": "animate_cancelled", "request_id": request_id})  # type: ignore[attr-defined]
        except Exception as exc:
            await self._animate_error(request_id, exc)

    async def _handle_animate_still(self, msg: dict[str, Any]) -> None:
        if await self._animate_refuse_web(msg):
            return
        request_id = msg.get("request_id")
        from engine import animate_data as data
        from engine.animate import AnimateCancelledError

        still_at = msg.get("still_at")
        if still_at is not None and (
            isinstance(still_at, bool) or not isinstance(still_at, (int, float))
        ):
            await self._animate_error(
                request_id, MovieRequestError("'still_at' must be a time in seconds")
            )
            return
        stills = data.default_cache_dir_for(None) / "stills"
        stills.mkdir(parents=True, exist_ok=True)
        path = stills / f"still-{int(time.time() * 1000)}-{os.getpid()}.png"

        def build() -> Any:
            raw = dict(msg.get("options") or {})
            raw["width"] = int(msg.get("width") or 960)
            raw["height"] = int(msg.get("height") or 540)
            return options_from_message(
                msg.get("inputs"),
                raw,
                still=str(path),
                still_at=float(still_at) if still_at is not None else None,
            )

        try:
            res, _opts = await self._animate_run(msg, build, rendering=True)
            png = path.read_bytes()
            await self.send(  # type: ignore[attr-defined]
                {
                    "type": "animate_still_result",
                    "request_id": request_id,
                    "png": base64.b64encode(png).decode("ascii"),
                    "still_at_s": res.output.get("still_at_s"),
                    "plan": _plan_payload(res),
                }
            )
        except AnimateCancelledError:
            await self.send({"type": "animate_cancelled", "request_id": request_id})  # type: ignore[attr-defined]
        except Exception as exc:
            await self._animate_error(request_id, exc)
        finally:
            for p in (path, path.with_suffix(".animate.json")):
                with contextlib.suppress(OSError):
                    p.unlink()

    async def _handle_animate_render(self, msg: dict[str, Any]) -> None:
        if await self._animate_refuse_web(msg):
            return
        request_id = msg.get("request_id")
        from engine.animate import AnimateCancelledError
        from engine.exporter import default_export_dir

        output = msg.get("output")
        if output is not None and not isinstance(output, str):
            await self._animate_error(
                request_id, MovieRequestError("'output' must be a file name or path")
            )
            return

        def build() -> Any:
            """The options with the output path settled: the user's name (in the export folder when
            it has no folder), else rxd's ``animate_<radars>_<time>.mp4``, in the export folder
            rather than the engine's working directory so the user can find it."""
            from engine import animate

            opts = options_from_message(msg.get("inputs"), msg.get("options"))
            if output:
                p = Path(output).expanduser()
                if not p.is_absolute():
                    p = default_export_dir() / p
            else:
                p = default_export_dir() / animate.default_output_name(animate.plan_inputs(opts))
            if p.suffix.lower() not in (".mp4", ".m4v"):
                p = p.with_suffix(".mp4")
            p.parent.mkdir(parents=True, exist_ok=True)
            opts.output = str(p)
            opts.command = opts.command.replace(
                "rxd animate", f"rxd animate -o {shlex.quote(str(p))}", 1
            )
            return opts

        try:
            loop = asyncio.get_running_loop()
            opts0 = await loop.run_in_executor(None, build)
            res, _opts = await self._animate_run(msg, lambda: opts0, rendering=True)
            out = res.output
            for key in ("video", "provenance", "still", "frames_written"):
                if out.get(key):
                    _WRITTEN.add(str(Path(out[key]).resolve()))
            await self.send(  # type: ignore[attr-defined]
                {
                    "type": "animate_render_result",
                    "request_id": request_id,
                    "plan": _plan_payload(res),
                    "video": out.get("video"),
                    "provenance": out.get("provenance"),
                    "frames_written": out.get("frames_written"),
                    "ffmpeg": out.get("ffmpeg"),
                    "video_bytes": out.get("video_bytes"),
                    "sound": res.sound,
                    "warnings": res.warnings,
                    "seconds": res.seconds,
                }
            )
        except AnimateCancelledError as exc:
            await self.send(  # type: ignore[attr-defined]
                {"type": "animate_cancelled", "request_id": request_id, "message": _friendly(exc)}
            )
        except Exception as exc:
            await self._animate_error(request_id, exc)

    async def _handle_animate_cancel(self, msg: dict[str, Any]) -> None:
        key = self._animate_id(msg)
        if key is not None:
            _cancel_event(key).set()

    async def _handle_animate_video(self, msg: dict[str, Any]) -> None:
        """The MP4 bytes of a film this engine wrote, as binary frames (the app's video player)."""
        if await self._animate_refuse_web(msg):
            return
        request_id = msg.get("request_id")
        path = msg.get("path")
        if not isinstance(path, str) or str(Path(path).expanduser().resolve()) not in _WRITTEN:
            await self._animate_error(
                request_id, MovieRequestError("only a film made in this session can be played here")
            )
            return
        p = Path(path).expanduser()
        try:
            data = await asyncio.get_running_loop().run_in_executor(None, p.read_bytes)
        except OSError as exc:
            await self._animate_error(request_id, exc)
            return
        await self._send_binary_chunks(  # type: ignore[attr-defined]
            [data],
            {
                "type": "animate_video",
                "request_id": request_id,
                "path": str(p),
                "mime": "video/mp4",
            },
            length_key="byte_length",
        )

    async def _handle_animate_copy(self, msg: dict[str, Any]) -> None:
        """Copy a film (and its record) to where the user chose: "Export a copy"."""
        if await self._animate_refuse_web(msg):
            return
        request_id = msg.get("request_id")
        src, dst = msg.get("src"), msg.get("dst")
        if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst:
            await self._animate_error(
                request_id, MovieRequestError("'src' and 'dst' must be paths")
            )
            return
        if str(Path(src).expanduser().resolve()) not in _WRITTEN:
            await self._animate_error(
                request_id, MovieRequestError("only a film made in this session can be copied here")
            )
            return
        s, d = Path(src).expanduser(), Path(dst).expanduser()
        if d.suffix.lower() not in (".mp4", ".m4v"):
            d = d.with_suffix(".mp4")

        def copy() -> dict[str, Any]:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(s, d)
            sidecar = s.with_suffix(".animate.json")
            out_sidecar = None
            if sidecar.is_file():
                out_sidecar = d.with_suffix(".animate.json")
                shutil.copyfile(sidecar, out_sidecar)
            return {"path": str(d), "sidecar": str(out_sidecar) if out_sidecar else None}

        try:
            done = await asyncio.get_running_loop().run_in_executor(None, copy)
        except OSError as exc:
            await self._animate_error(request_id, exc)
            return
        await self.send({"type": "animate_copied", "request_id": request_id, **done})  # type: ignore[attr-defined]


def options_as_dict(opts: Any) -> dict[str, Any]:
    """The options as plain JSON (for a record or a test)."""
    return {k: v for k, v in dataclasses.asdict(opts).items() if k != "files"}

"""The ``rxd animate`` command line: options, input handling and output (see docs/ANIMATE.md).

Kept out of :mod:`engine.cli` so that file only gets a few lines for the new command. Standard
library only at import time; the engine is imported when the command runs.
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import os
import re
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

EPILOG = """\
examples:
  rxd animate -i 'mayfield/*/*_V06' -o mayfield.mp4
  rxd animate -i mayfield -o mayfield.mp4 --camera top-down --duration 30
  rxd animate -i mayfield -o mayfield.mp4 --realistic --look-preset "Golden hour" --set exposure=1.4
  rxd animate -i mayfield -o mayfield.mp4 --camera my_camera.toml --look my_look.toml
  rxd animate -i mayfield -p still.png --still-at 12
  rxd animate -i mayfield --no-render --json
  rxd animate -i mayfield -o storm.mp4 --realistic --light night --sound --flashes-from-glm glm/
  rxd animate -i laura --reference KLCH --tolerance-s 400 --track linear --camera wide -o laura.mp4

The files are grouped by radar and time; every time step takes each radar's nearest volume within
--tolerance-s (files that are not used are listed with the reason). The strongest storm is found
and tracked, and a 3D camera flies around it in the app's own renderer (headless Chrome). Frames
between two volumes are interpolated and are labelled so on the screen. Needs the optional extra:
pip install 'radarx-desktop-engine[video]' (Playwright and PyAV), the system Chrome, and the built
app (npm run build); --no-render needs none of them. Writes the MP4 at the end, atomically; an
interrupted job resumes from its cache. --sound adds a SYNTHESISED sound track (wind, rain, thunder; not
a recording). docs/ANIMATE.md has the camera file, the look keys and the sound."""


def add_parser(
    command: Callable[..., argparse.ArgumentParser],
    add_input: Callable[..., None],
    add_output: Callable[..., None],
    add_plot: Callable[..., None],
) -> argparse.ArgumentParser:
    """Register ``animate`` on the ``rxd`` parser (called by ``build_parser``)."""
    p = command(
        "animate",
        "Make a storm-centred 3D animation (MP4) from many volumes of one or more radars.",
        EPILOG,
    )
    add_input(p, "radar files of one or more radars over time (files, globs, folders)")
    add_output(p, "the MP4 (default animate_<radars>_<time>.mp4), or a folder for a PNG sequence")
    add_plot(p, "write ONE frame as a PNG (see --still-at); with only -p no video is rendered")
    p.add_argument(
        "--still-at",
        type=float,
        metavar="S",
        help="clip time (s) of the -p still (default: the middle)",
    )

    g = p.add_argument_group("time steps (which volumes)")
    g.add_argument(
        "--reference",
        default="auto",
        metavar="RADAR",
        help="radar whose volume times set the steps (default: the one that gives the most)",
    )
    g.add_argument(
        "--tolerance-s",
        type=float,
        default=330.0,
        metavar="S",
        help="largest time difference between a step and a radar's volume (330)",
    )
    g.add_argument(
        "--min-radars",
        type=int,
        metavar="N",
        help="radars a step needs (default 2, or 1 for a single radar)",
    )
    g.add_argument(
        "--min-step-s", type=float, metavar="S", help="drop steps closer together than this"
    )
    g.add_argument("--start", metavar="TIME", help="first time to use, e.g. 2021-12-11T02:00:00Z")
    g.add_argument("--end", metavar="TIME", help="last time to use")
    g.add_argument("--max-steps", type=int, metavar="N", help="use at most the first N steps")

    g = p.add_argument_group("the storm and the grids")
    g.add_argument(
        "--cell-dbz",
        type=float,
        default=45.0,
        metavar="DBZ",
        help="reflectivity of a convective cell (45)",
    )
    g.add_argument(
        "--track",
        choices=["cells", "linear", "eye"],
        default="cells",
        help="cell positions (default), their straight-line fit, or the eye of a cyclone",
    )
    g.add_argument(
        "--crop-km",
        type=float,
        default=60.0,
        metavar="KM",
        help="half width of the shown box around the storm (60)",
    )
    g.add_argument(
        "--detail-m", type=float, default=500.0, metavar="M", help="grid cell size (500)"
    )
    g.add_argument(
        "--winds",
        action="store_true",
        help="also retrieve multi-Doppler winds per step (slower) so the echo is carried along the observed flow",
    )
    g.add_argument(
        "--cache-dir",
        metavar="DIR",
        help="gridded volumes and frames (default ~/.cache/radarx-desktop/animate, or XRADAR_ANIMATE_CACHE)",
    )
    g.add_argument("--fresh", action="store_true", help="ignore the cache and grid again")

    g = p.add_argument_group("the clip")
    g.add_argument(
        "--duration",
        type=float,
        metavar="S",
        help="clip length in seconds (default 36, or the camera file's)",
    )
    g.add_argument(
        "--timelapse",
        type=float,
        metavar="X",
        help="set the length from the pace: data span / X + 3 s",
    )
    g.add_argument("--fps", type=int, default=30, help="frames per second (30)")
    g.add_argument("--size", default="1920x1080", metavar="WxH", help="frame size (1920x1080)")
    g.add_argument(
        "--camera",
        metavar="NAME|FILE",
        help="orbit (default), wide, top-down, window, flight, or a camera file (.toml / .json)",
    )
    g.add_argument(
        "--target-lag",
        type=float,
        metavar="0..1",
        help="how much of the storm's move the camera follows (0.3)",
    )
    g.add_argument(
        "--fov", type=float, metavar="DEG", help="vertical field of view for the whole clip"
    )
    g.add_argument("--view-km", type=float, metavar="KM", help="scene size of --camera wide (330)")
    g.add_argument("--vexag", type=float, help="vertical exaggeration (4; 1 for window and flight)")
    g.add_argument("--observer", metavar="X,Y", help="window camera: position in km of the scene")
    g.add_argument(
        "--observer-dist",
        type=float,
        metavar="KM",
        help="window camera: km from the storm track (10)",
    )
    g.add_argument(
        "--observer-upwind",
        type=float,
        metavar="KM",
        help="window camera: km upwind of the track's middle (4)",
    )
    g.add_argument(
        "--observer-side", choices=["left", "right"], help="window camera: side of the track"
    )
    g.add_argument("--eye-m", type=float, metavar="M", help="window camera: eye height (10)")
    g.add_argument(
        "--pan",
        type=float,
        metavar="0..1",
        help="window camera: how much it turns with the storm (0.3)",
    )
    g.add_argument(
        "--pitch",
        type=float,
        metavar="DEG",
        help="window camera: look up by this many degrees (12)",
    )

    g = p.add_argument_group("the look (docs/REALISTIC_3D.md)")
    g.add_argument("--look", metavar="FILE", help="a look file (.toml / .json) with any look keys")
    g.add_argument(
        "--look-preset",
        metavar="NAME",
        help="a built-in look, e.g. 'Golden hour' (a file and --set go on top)",
    )
    g.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="one look setting, highest priority (repeatable)",
    )
    g.add_argument(
        "--realistic",
        action="store_true",
        help="terrain with satellite imagery and the stylised storm (needs the tile servers or a tile cache)",
    )
    g.add_argument(
        "--ground", choices=["map", "realistic"], help="flat map (default) or terrain with imagery"
    )
    g.add_argument(
        "--storm-style",
        choices=["scientific", "realistic"],
        help="the dBZ colour scale (default) or the stylised look",
    )
    g.add_argument(
        "--palette",
        choices=["white", "soft", "stormy", "cyclone"],
        help="palette of the realistic storm",
    )
    g.add_argument(
        "--light",
        choices=["daylight", "dusk", "overcast", "night"],
        help="light of the realistic storm",
    )
    g.add_argument("--map-theme", choices=["dark", "light"], default="dark")
    g.add_argument("--basemap", default="osm", help="basemap provider id (osm)")
    g.add_argument("--title", help="title at the top left (default: the radars and date)")
    g.add_argument("--subtitle", help="line under the title")
    g.add_argument("--credit", help="credit line at the bottom (default names the radars)")
    g.add_argument(
        "--sea",
        action="store_true",
        help="ocean with whitecaps from the retrieved surface wind (needs --winds and the realistic ground)",
    )
    g.add_argument(
        "--lightning-model",
        action="store_true",
        help="draw MODELLED lightning (labelled as not observed); off by default",
    )

    g = p.add_argument_group("sound (synthesised from the data, not a recording; off by default)")
    g.add_argument(
        "--sound",
        action="store_true",
        help="write an AAC 48 kHz stereo track into the MP4: wind from the retrieved winds, rain from the reflectivity, thunder from the flashes named below (none without them)",
    )
    g.add_argument(
        "--sound-listener",
        metavar="X,Y",
        help="where the virtual listener stands: km east and north of the middle of the storm's track "
        "(default: the window-camera place, 10 km to the right of the track and 4 km upwind; with --camera "
        "window or flight the camera itself)",
    )
    g.add_argument(
        "--sound-compress",
        type=float,
        metavar="N",
        help="the delay between a flash and its thunder is distance / 343 m/s divided by N (1 = real physics; "
        "default: the film's time-lapse factor, so the thunder keeps its place in the film's own time)",
    )
    g.add_argument(
        "--sound-seed", type=int, metavar="N", help="seed of the noise (7): same seed, same sound"
    )
    g.add_argument(
        "--sound-thunder-db",
        type=float,
        metavar="DB",
        help="thunder level relative to the default mix (0): the default puts the thunder clearly above the wind and rain bed",
    )
    g.add_argument(
        "--draw-flashes",
        action="store_true",
        help="draw the flashes the thunder follows in the picture, at the time the thunder is timed to (labelled OBSERVED GLM / from the file / MODELLED)",
    )
    g.add_argument(
        "--flashes",
        metavar="FILE",
        help="thunder follows the flashes of a CSV (columns time_utc, lat, lon; optional type CG|IC, energy), used as given",
    )
    g.add_argument(
        "--flashes-from-glm",
        action="append",
        default=[],
        metavar="DIR",
        help="thunder follows OBSERVED GOES GLM flashes read from local Level 2 LCFA .nc files (folder, files or pattern; "
        "nothing is downloaded): the strongest flash of every --glm-bin-s within --glm-radius-km of the tracked storm; "
        "the chosen flashes are also written next to the film as NAME.flashes.csv",
    )
    g.add_argument(
        "--glm-radius-km",
        type=float,
        metavar="KM",
        help="largest distance of a GLM flash from the tracked storm centre (30)",
    )
    g.add_argument(
        "--glm-bin-s",
        type=float,
        metavar="S",
        help="keep the strongest GLM flash of every S seconds of data time (default: the time-lapse factor, about one thunderclap per film second)",
    )

    g = p.add_argument_group("rendering and output")
    g.add_argument(
        "--tile-cache",
        metavar="DIR",
        help="folder of cached map tiles (default ~/.cache/radarx-desktop/tiles)",
    )
    g.add_argument("--offline", action="store_true", help="use cached tiles only, no network")
    g.add_argument(
        "--app-dist",
        metavar="DIR",
        help="the built app (default: <checkout>/dist or XRADAR_APP_DIST)",
    )
    g.add_argument(
        "--browser",
        default="chrome",
        metavar="CHANNEL",
        help="Playwright browser channel (chrome); '' = Playwright's own Chromium",
    )
    g.add_argument(
        "--software",
        action="store_true",
        help="no GPU: Chrome's software renderer (works anywhere, very slow; also the automatic fallback)",
    )
    g.add_argument("--crf", type=int, default=18, help="x264 quality, lower is better (18)")
    g.add_argument("--x264-preset", default="medium", help="x264 speed preset (medium)")
    g.add_argument(
        "--frame-format",
        choices=["png", "jpg"],
        default="png",
        help="frames on disk: png (lossless, default) or jpg (about 3x smaller)",
    )
    g.add_argument(
        "--keep-frames",
        action="store_true",
        help="keep the rendered frames after the MP4 is written",
    )
    g.add_argument(
        "--no-render",
        action="store_true",
        help="grid, track and plan only: print the report, draw nothing (needs no browser)",
    )
    p.set_defaults(func=cmd_animate)
    return p


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def expand_animate_inputs(items: Sequence[str]) -> list[str]:
    """Like ``rxd``'s ``-i`` but a case folder that holds one folder per radar works too.

    A folder with radar files in it gives those files. A folder with only sub-folders (one per
    radar) gives the files in them whose names carry a radar and a time (``KHPX20211211_015357_V06``),
    so result folders next to the radars are not picked up.
    """
    from engine import animate_plan as plan
    from engine import cli

    out: list[str] = []
    plain: list[str] = []

    def flush() -> None:
        if plain:
            out.extend(cli.expand_inputs(plain))
            plain.clear()

    for raw in items:
        path = os.path.expanduser(raw)
        if raw != "-" and os.path.isdir(path):
            entries = [e for e in sorted(os.listdir(path)) if not e.startswith(".")]
            direct = [
                e
                for e in entries
                if os.path.isfile(os.path.join(path, e))
                and Path(e).suffix.lower() not in cli.NON_RADAR_SUFFIXES
            ]
            subdirs = [e for e in entries if os.path.isdir(os.path.join(path, e))]
            if not direct and subdirs:
                found = []
                for d in subdirs:
                    for e in sorted(os.listdir(os.path.join(path, d))):
                        fp = os.path.join(path, d, e)
                        if os.path.isfile(fp) and plan.parse_volume_name(e):
                            found.append(fp)
                if not found:
                    from engine.api_results import XrdInputError

                    raise XrdInputError(
                        f"No radar files found in the sub-folders of {raw}. Their names should look "
                        "like KHPX20211211_015357_V06; or give the files with -i."
                    )
                flush()
                out.extend(found)
                continue
        plain.append(raw)
    flush()
    seen: set[str] = set()
    return [f for f in out if not (f in seen or seen.add(f))]


def parse_size(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*[xX*,]\s*(\d+)\s*", text)
    if not m:
        from engine.animate_plan import AnimateConfigError

        raise AnimateConfigError(f"--size {text!r}: use WIDTHxHEIGHT, for example 1920x1080")
    return int(m[1]), int(m[2])


def options_from_args(args: argparse.Namespace, files: list[str]) -> Any:
    from engine import animate
    from engine.animate_camera import observer_settings
    from engine.animate_sound import parse_listener

    width, height = parse_size(args.size)
    obs: dict[str, Any] = {}
    if args.observer:
        try:
            x, y = (float(v) for v in args.observer.split(","))
        except ValueError:
            from engine.animate_plan import AnimateConfigError

            raise AnimateConfigError(
                f"--observer {args.observer!r}: use X,Y in km, e.g. 12,-4"
            ) from None
        obs.update(x=x, y=y)
    for key, val in (
        ("dist_km", args.observer_dist),
        ("upwind_km", args.observer_upwind),
        ("eye_m", args.eye_m),
        ("pan", args.pan),
        ("pitch_deg", args.pitch),
        ("side", args.observer_side),
    ):
        if val is not None:
            obs[key] = val
    outputs = list(args.output)
    if len(outputs) > 1:
        from engine.animate_plan import AnimateConfigError

        raise AnimateConfigError("animate writes one result: give -o once")
    return animate.AnimateOptions(
        files=files,
        output=outputs[0] if outputs else None,
        still=args.plot,
        still_at=args.still_at,
        reference=None if (args.reference or "auto") == "auto" else args.reference.upper(),
        tolerance_s=args.tolerance_s,
        min_radars=args.min_radars,
        min_step_s=args.min_step_s,
        start=args.start,
        end=args.end,
        max_steps=args.max_steps,
        detail_m=args.detail_m,
        crop_km=args.crop_km,
        cell_dbz=args.cell_dbz,
        track=args.track,
        winds=args.winds,
        cache_dir=args.cache_dir,
        fresh=args.fresh,
        duration=args.duration,
        timelapse=args.timelapse,
        fps=args.fps,
        width=width,
        height=height,
        camera=args.camera,
        target_lag=args.target_lag,
        fov=args.fov,
        view_km=args.view_km,
        observer=observer_settings(obs) if obs else {},
        vexag=args.vexag,
        look_preset=args.look_preset,
        look_file=args.look,
        set_values=list(args.set),
        realistic=args.realistic,
        ground=args.ground,
        storm_style=args.storm_style,
        palette=args.palette,
        light=args.light,
        map_theme=args.map_theme,
        basemap=args.basemap,
        title=args.title,
        subtitle=args.subtitle,
        credit=args.credit,
        lightning_model=args.lightning_model,
        sea=args.sea,
        sound=args.sound,
        sound_listener=parse_listener(args.sound_listener) if args.sound_listener else None,
        sound_compress=args.sound_compress,
        sound_seed=args.sound_seed,
        flashes=args.flashes,
        flashes_from_glm=list(args.flashes_from_glm),
        glm_radius_km=args.glm_radius_km,
        glm_bin_s=args.glm_bin_s,
        draw_flashes=args.draw_flashes,
        sound_thunder_db=args.sound_thunder_db,
        tile_cache=args.tile_cache,
        offline=args.offline,
        app_dist=args.app_dist,
        browser=args.browser,
        software=args.software,
        command=shlex.join(["rxd", *getattr(args, "argv", [])]),
        crf=args.crf,
        preset=args.x264_preset,
        frame_format=args.frame_format,
        keep_frames=args.keep_frames,
        no_render=args.no_render,
    )


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def cmd_animate(args: argparse.Namespace, out: Any) -> int:
    from engine import animate, cli
    from engine.animate_plan import AnimateConfigError, AnimatePlanError
    from engine.api_results import XrdInputError

    items = [*args.input, *getattr(args, "extra", [])]
    if not items:
        raise cli.UsageError(
            "animate needs radar files: give them with -i (for example: "
            "rxd animate -i 'mayfield/*/*_V06' -o storm.mp4)"
        )
    try:
        files = expand_animate_inputs(items)
        opts = options_from_args(args, files)
        res = animate.run(opts, note=out.note, detail=lambda m: out.detail(m))
    except AnimateConfigError as exc:
        raise cli.UsageError(str(exc)) from exc
    except AnimatePlanError as exc:
        raise XrdInputError(str(exc)) from exc

    o = res.output
    if opts.output is None and opts.still is None and o.get("video"):
        out.chose(f"{Path(o['video']).name} as the output (add -o to choose)", "-o", o["video"])
    if out.json:
        out.files += [
            v
            for v in (
                o.get("video"),
                o.get("still"),
                o.get("frames_written"),
                o.get("provenance"),
                o.get("sound_flashes"),
            )
            if v
        ]
        payload = res.to_dict()
        payload["files"] = out.files
        out.emit_json(payload)
        return cli.EXIT_OK
    if not out.quiet:
        out.result(animate.format_report(res, cli._table))
    for key in ("video", "still", "frames_written", "provenance", "sound_flashes"):
        if o.get(key):
            out.wrote(o[key])
    if o.get("ffmpeg"):
        out.note("to make the MP4 from those frames: " + o["ffmpeg"])
    if not o.get("rendered", True):
        out.note(
            "nothing was drawn (--no-render): this is the plan; remove --no-render to render it"
        )
    return cli.EXIT_OK

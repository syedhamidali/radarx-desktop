"""``rxd animate``: a storm-centred 3D animation from many radar volumes.

This module is the orchestrator. The pieces it ties together:

* :mod:`engine.animate_plan`    groups the files by radar and time, picks the time steps, plans frames
* :mod:`engine.animate_data`    grids one time step at a time (bounded memory), tracks the storm, caches
* :mod:`engine.animate_camera`  keyframes, easing, presets, camera files
* :mod:`engine.animate_render`  the app's renderer in headless Chrome, frames, the MP4

and returns an :class:`AnimateResult` that the command line prints as text or JSON. Everything that
is *decided* (the steps, the track, the frame plan, the camera path) is computed and reported
without a browser, so a job can be checked with ``--no-render`` before it is rendered.
"""

# ruff: noqa: E501
from __future__ import annotations

import contextlib
import json
import math
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine import animate_camera as cam
from engine import animate_data as data
from engine import animate_plan as plan
from engine.animate_plan import AnimateConfigError, AnimatePlanError

Note = Callable[[str], None]


@dataclass
class AnimateOptions:
    """Everything ``rxd animate`` can be told (the command line options, by their long names)."""

    files: list[str] = field(default_factory=list)
    output: str | None = None  # an .mp4, or a folder (PNG sequence)
    still: str | None = None  # -p: one frame as a PNG
    still_at: float | None = None  # clip time (s) of the still; default: the middle
    # time steps
    reference: str | None = None
    tolerance_s: float = plan.TOLERANCE_S
    min_radars: int | None = None
    min_step_s: float | None = None
    start: str | None = None
    end: str | None = None
    max_steps: int | None = None
    # gridding and the storm
    detail_m: float = 500.0
    crop_km: float = 60.0
    cell_dbz: float = 45.0
    track: str = "cells"
    winds: bool = False
    cache_dir: str | None = None
    fresh: bool = False
    # the clip
    duration: float | None = None
    timelapse: float | None = None
    fps: int = 30
    width: int = 1920
    height: int = 1080
    camera: str | None = None
    target_lag: float | None = None
    fov: float | None = None
    view_km: float | None = None
    observer: dict[str, Any] = field(default_factory=dict)
    vexag: float | None = None
    # look
    look_preset: str | None = None
    look_file: str | None = None
    set_values: list[str] = field(default_factory=list)
    realistic: bool = False
    ground: str | None = None  # "map" | "realistic"
    storm_style: str | None = None  # "scientific" | "realistic"
    palette: str | None = None
    light: str | None = None
    map_theme: str = "dark"
    basemap: str = "osm"
    title: str | None = None
    subtitle: str | None = None
    credit: str | None = None
    lightning_model: bool = False
    sea: bool = False
    # rendering and output
    tile_cache: str | None = None
    offline: bool = False
    app_dist: str | None = None
    browser: str = "chrome"
    software: bool = False
    command: str = ""  # the command line, recorded in the provenance file
    crf: int = 18
    preset: str = "medium"
    frame_format: str = "png"
    keep_frames: bool = False
    no_render: bool = False


@dataclass
class AnimateResult:
    """What a run decided and did; ``to_dict()`` is the JSON of ``--json``."""

    steps: plan.StepPlan
    bundle: data.Bundle
    frames: list[plan.Frame]
    frame_summary: dict[str, Any]
    camera: cam.CameraPlan
    camera_path: dict[str, Any] | None
    look: dict[str, Any] | None
    options: AnimateOptions
    fps: int
    duration: float
    size: tuple[int, int]
    cache: data.GridCache
    output: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0

    # -- the storm -----------------------------------------------------------------------
    def storm_rows(self) -> list[dict[str, Any]]:
        m = self.bundle.manifest
        track = m["storm"]["track_km"]
        ll = m["storm"]["track_lonlat"]
        rows = []
        for i, st in enumerate(m["steps"]):
            speed = None
            if i:
                dt = st["t_s"] - m["steps"][i - 1]["t_s"]
                d = math.hypot(track[i][0] - track[i - 1][0], track[i][1] - track[i - 1][1])
                speed = d * 1000.0 / dt if dt > 0 else None
            rows.append(
                {
                    "index": i,
                    "time": self.steps.steps[i].time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "x_km": track[i][0],
                    "y_km": track[i][1],
                    "lon": ll[i][0],
                    "lat": ll[i][1],
                    "speed_ms_since_previous": speed,
                    "max_dbz": st["storm"]["max_dbz"],
                    "area_km2": st["storm"]["area_km2"],
                    "top_km": st["storm"]["top_km"],
                }
            )
        return rows

    def storm_summary(self) -> dict[str, Any]:
        m = self.bundle.manifest
        track = m["storm"]["track_km"]
        d = math.hypot(track[-1][0] - track[0][0], track[-1][1] - track[0][1])
        span = self.steps.times_s[-1]
        return {
            "tracker": self.options.track,
            "displacement_km": d,
            "heading_deg": m["storm"]["heading_deg"],
            "mean_speed_ms": m["storm"]["speed_ms"],
            "span_s": span,
            "origin": m["origin"],
            "track": self.storm_rows(),
        }

    def to_dict(self) -> dict[str, Any]:
        plan_ = self.steps
        cache = self.cache
        return {
            "command": "animate",
            "inputs": {
                "radars": plan_.radars,
                "used_radars": plan_.used_radars,
                "reference": plan_.reference,
                "tolerance_s": plan_.tolerance_s,
                "steps": len(plan_.steps),
                "skipped": [s.as_dict() for s in plan_.skipped],
            },
            "steps": plan.step_table(plan_),
            "radar_offsets": plan.offset_summary(plan_),
            "storm": self.storm_summary(),
            "frames": {
                **self.frame_summary,
                "fps": self.fps,
                "duration_s": self.duration,
                "timelapse": plan.timelapse_of(plan_.times_s[-1], self.duration),
                "size": list(self.size),
            },
            "camera": {
                "mode": self.camera.mode,
                "source": self.camera.source,
                "target_lag": self.camera.target_lag,
                "path": self.camera_path,
            },
            "look": self.look,
            "grid": {
                "detail_m": self.options.detail_m,
                "cache_dir": str(cache.root),
                "hits": dict(cache.hits),
                "computed": dict(cache.computed),
                "seconds": round(self.bundle.stats.seconds, 1),
                "peak_rss_mb": self.bundle.stats.peak_rss_mb,
                "fine_mb_per_step": self.bundle.stats.fine_mb_per_step,
            },
            "output": self.output,
            "warnings": self.warnings,
            "seconds": round(self.seconds, 1),
        }


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def window_time(text: str | None, label: str) -> datetime | None:
    if not text:
        return None
    try:
        return plan.parse_time(text)
    except AnimatePlanError as exc:
        raise AnimateConfigError(f"{label}: {exc}") from exc


def check_options(o: AnimateOptions) -> None:
    """Refuse nonsense early, with the fix in the message (before anything is read or rendered)."""
    if o.fps < 1 or o.fps > 120:
        raise AnimateConfigError(
            f"--fps {o.fps} is out of range: use 1 to 120 (24 or 30 are usual)"
        )
    if o.width < 160 or o.height < 90 or o.width > 7680 or o.height > 4320:
        raise AnimateConfigError(
            f"--size {o.width}x{o.height} is out of range: 160x90 to 7680x4320"
        )
    if o.duration is not None and o.timelapse is not None:
        raise AnimateConfigError("give --duration or --timelapse, not both")
    if o.duration is not None:
        cam.check_duration(o.duration)
    if o.tolerance_s <= 0:
        raise AnimateConfigError("--tolerance-s must be positive (seconds)")
    if o.track not in ("cells", "linear", "eye"):
        raise AnimateConfigError(f"--track {o.track!r}: use cells, linear or eye")
    if o.frame_format not in ("png", "jpg"):
        raise AnimateConfigError(f"--frame-format {o.frame_format!r}: use png or jpg")
    if not 0 <= o.crf <= 51:
        raise AnimateConfigError(
            "--crf must be between 0 (lossless) and 51; 18 is visually lossless"
        )
    if o.detail_m < 250 or o.detail_m > 5000:
        raise AnimateConfigError("--detail-m must be between 250 and 5000 metres")
    if o.target_lag is not None and not 0 <= o.target_lag <= 1:
        raise AnimateConfigError(
            "--target-lag must be between 0 (fixed) and 1 (locked on the storm)"
        )
    from engine import animate_render as render

    render.look_settings(render.LookArgs(o.look_preset, o.look_file, o.set_values))  # format only
    if o.ground not in (None, "map", "realistic"):
        raise AnimateConfigError("--ground must be map or realistic")
    if o.storm_style not in (None, "scientific", "realistic"):
        raise AnimateConfigError("--storm-style must be scientific or realistic")


def default_output_name(refs_plan: plan.StepPlan) -> str:
    first = refs_plan.steps[0].time
    radars = "-".join(refs_plan.used_radars[:3]) + (
        "-etc" if len(refs_plan.used_radars) > 3 else ""
    )
    return f"animate_{radars}_{first:%Y%m%dT%H%M}.mp4"


def radar_credit(o: AnimateOptions, sp: plan.StepPlan) -> str:
    if o.credit:
        return o.credit
    radars = ", ".join(sp.used_radars)
    nexrad = all(
        v.source == "name" and len(v.radar) == 4 and v.radar[0] in "KPTJ"
        for st in sp.steps
        for v, _ in st.files.values()
    )
    return f"Radar: {radars} (NEXRAD Level II, NOAA)" if nexrad else f"Radar data: {radars}"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def plan_inputs(
    o: AnimateOptions, meta: Callable[[Path], tuple[str, datetime]] | None = None
) -> plan.StepPlan:
    """Scan, group and choose the time steps (no radar data is decoded for NEXRAD style names)."""
    if not o.files:
        raise AnimatePlanError("no input files: give them with -i (files, globs or folders)")
    cache = None
    if meta is None:  # files whose names carry no time are opened once; the answer is remembered
        cache = plan.MetaCache(data.default_cache_dir_for(o.cache_dir) / "scan.json")
        meta = cache.wrap(plan.read_file_meta)
    refs, unreadable = plan.scan_files(o.files, meta)
    if cache is not None:
        cache.save()
    if not refs:
        why = "; ".join(f"{s.what}: {s.reason}" for s in unreadable[:3])
        raise AnimatePlanError(
            f"none of the {len(o.files)} input file(s) could be placed in time. {why}"
        )
    groups = plan.group_by_radar(refs)
    sp = plan.choose_steps(
        groups,
        o.reference,
        tolerance_s=o.tolerance_s,
        min_radars=o.min_radars,
        min_step_s=o.min_step_s,
        start=window_time(o.start, "--start"),
        end=window_time(o.end, "--end"),
        max_steps=o.max_steps,
    )
    sp.skipped = unreadable + sp.skipped
    return sp


def drop_unreadable(
    sp: plan.StepPlan, manifest: dict[str, Any], unreadable: dict[str, str]
) -> dict[str, str]:
    """Take the radars whose files could not be read out of the plan and the manifest, and list their
    files as not used with the reason. Returns ``{radar: reason}`` for those that were in the plan."""
    gone = {r: why for r, why in unreadable.items() if r in sp.used_radars}
    if not gone:
        return {}
    radars_before = list(sp.used_radars)
    for st, ms in zip(sp.steps, manifest["steps"], strict=True):
        for radar, why in gone.items():
            if radar in st.files:
                vol, _off = st.files.pop(radar)
                ms["files"].pop(radar, None)
                sp.skipped.append(
                    plan.Skip(vol.name, f"could not be read: {why}", radar, vol.time.isoformat())
                )
    seen: set[str] = set()
    sp.skipped[:] = [s for s in sp.skipped if not (s.what in seen or seen.add(s.what))]
    first = sp.steps[0].time
    if manifest.get("title") == data.default_title_for(radars_before, first):
        manifest["title"] = data.default_title_for(sp.used_radars, first)
    manifest["case"] = ", ".join(sp.used_radars)
    return gone


def page_settings(
    o: AnimateOptions, cp: cam.CameraPlan, sp: plan.StepPlan, fps: int, duration: float
) -> dict[str, Any]:
    """Settings handed to the clip page (``ClipSettings`` in stormClip.ts)."""
    from engine import animate_render as render

    s: dict[str, Any] = {
        "fps": fps,
        "duration": duration,
        "mapTheme": o.map_theme,
        "basemap": o.basemap,
        "tileBase": "/tiles",
        "flow": True,
        "lightning": "auto" if o.lightning_model else "off",
        "sea": bool(o.sea),
        "audio": False,
        "observedTolS": plan.OBSERVED_TOL_S,
        "radarCredit": radar_credit(o, sp),
        **cp.page_settings(),
        **render.look_settings(
            render.LookArgs(o.look_preset, o.look_file, o.set_values),
        ),
    }
    s["ground"] = "realistic" if o.realistic else (o.ground or "map")
    s["stormStyle"] = "realistic" if o.realistic else (o.storm_style or "scientific")
    if o.vexag is not None:
        s["vexag"] = o.vexag
    if o.palette:
        s["palette"] = o.palette
    if o.light:
        s["light"] = o.light
    if o.title:
        s["title"] = o.title
    if o.subtitle:
        s["subtitle"] = o.subtitle
    return s


def run(
    o: AnimateOptions,
    note: Note = lambda _m: None,
    detail: Note = lambda _m: None,
    gridder: data.Gridder | None = None,
    meta: Callable[[Path], tuple[str, datetime]] | None = None,
) -> AnimateResult:
    """Plan, grid, track, plan the frames and (unless ``no_render``) render and write the output.

    Needs a signed-in account like everything else (``AccountRequired`` otherwise, docs/ACCOUNTS.md).
    """
    from engine.account import activity, require_account

    require_account()
    result = _run(o, note, detail, gridder, meta)
    activity.record("animate_run")
    return result


def _run(
    o: AnimateOptions,
    note: Note,
    detail: Note,
    gridder: data.Gridder | None,
    meta: Callable[[Path], tuple[str, datetime]] | None,
) -> AnimateResult:
    t_start = time.perf_counter()
    from engine import animate_render as render

    check_options(o)
    spec = cam.load_camera(o.camera, o.view_km)
    # fail early on a missing browser or app, before minutes of gridding
    rendering = not o.no_render
    dist: Path | None = None
    if rendering:
        render.require_playwright()
        dist = render.find_frontend(o.app_dist)

    sp = plan_inputs(o, meta)
    note(
        f"{len(sp.steps)} time steps from {len(sp.used_radars)} radar(s) ({', '.join(sp.used_radars)}); "
        f"reference {sp.reference}"
    )
    cache = data.GridCache(o.cache_dir, fresh=o.fresh)
    opts = data.GridOptions(
        detail_m=o.detail_m,
        crop_half_km=o.crop_km,
        cell_dbz=o.cell_dbz,
        track_mode=o.track,
        eye=spec.preset == "flight",
        winds=o.winds,
    )

    # the look is checked in the page first: a typo should not cost a gridding run
    base_settings: dict[str, Any] | None = None
    look_changed: dict[str, Any] | None = None
    if rendering:
        assert dist is not None
        probe = page_settings(o, cam.resolve_camera(spec, 36.0, 0.0), sp, o.fps, 36.0)
        with render.Renderer(
            dist, 320, 180, channel=o.browser, tile_cache=o.tile_cache, offline=True
        ) as r:
            look_changed = r.check_look(
                {
                    k: v
                    for k, v in probe.items()
                    if k.startswith("look") or k in ("palette", "light")
                }
            )
        base_settings = probe

    g = gridder or data.EngineGridder(log=detail)
    bundle = data.build_bundle(sp, opts, cache, g, log=detail)
    m = bundle.manifest
    left_out = drop_unreadable(sp, m, bundle.unreadable)
    heading = m["storm"]["heading_deg"]
    span = sp.times_s[-1]

    duration = o.duration
    if o.timelapse is not None:
        duration = plan.duration_for_timelapse(span, o.timelapse)
    cp = cam.resolve_camera(
        spec,
        duration,
        heading,
        target_lag=o.target_lag,
        fov=o.fov,
        observer=o.observer or None,
    )
    duration = cp.duration
    cam.check_duration(duration)
    frames = plan.plan_frames(duration, o.fps, sp.times_s)
    summary = plan.summarise_frames(frames, sp.times_s)
    path = cam.describe_path(cp.keys, duration, o.fps) if cp.keys else None
    warnings: list[str] = [
        f"{radar} left out: its files could not be used ({why})" for radar, why in left_out.items()
    ]
    if cp.keys and path and path["ends_after_clip"]:
        warnings.append(
            f"the camera file has keys after the end of the clip ({cp.keys[-1].t:g} s > "
            f"{duration:g} s): they are never reached; set --duration {cp.keys[-1].t:g}"
        )
    if path and path["holds_last_pose_s"] > 0.5:
        warnings.append(
            f"the camera stays at its last pose for the final {path['holds_last_pose_s']:.1f} s"
        )
    if summary["interpolated_share"] > 0.9 and summary["longest_gap_s"] > 600:
        warnings.append(
            f"{100 * summary['interpolated_share']:.0f}% of the frames are interpolated and the "
            f"longest gap between volumes is {summary['longest_gap_s'] / 60:.0f} min: the echo "
            "between volumes is carried, not observed"
        )
    if not (bundle.manifest["animate"]["winds"]):
        detail("interpolation: translation along the storm motion (no wind retrieval)")

    res = AnimateResult(
        sp,
        bundle,
        frames,
        summary,
        cp,
        path,
        look_changed,
        o,
        o.fps,
        duration,
        (o.width, o.height),
        cache,
        warnings=warnings,
    )
    if not rendering:
        res.output = {"rendered": False, "reason": "--no-render"}
        res.seconds = time.perf_counter() - t_start
        return res

    assert dist is not None and base_settings is not None
    _render(res, dist, page_settings(o, cp, sp, o.fps, duration), note, detail)
    res.seconds = time.perf_counter() - t_start
    return res


def _render(
    res: AnimateResult, dist: Path, settings: dict[str, Any], note: Note, detail: Note
) -> None:
    from engine import animate_render as render

    o = res.options
    cache = res.cache
    m = res.bundle.manifest
    n = len(res.frames)
    w, h = res.size
    ext = o.frame_format
    jobs = cache.root / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    digest = render.job_key({"manifest": m, "settings": settings})
    manifest_path = jobs / f"manifest-{digest}.json"
    tmp = manifest_path.with_name(manifest_path.name + ".part")
    tmp.write_text(json.dumps(m))
    tmp.replace(manifest_path)
    key = render.job_key(
        {
            "manifest": digest,
            "size": [w, h],
            "fps": res.fps,
            "n": n,
            "ext": ext,
            "app": render.frontend_fingerprint(dist),
        }
    )
    frames_dir = jobs / key / "frames"

    out = res.output
    out.update({"rendered": True, "frames_dir": str(frames_dir), "job": key})
    want_video = o.output is not None or o.still is None
    video_path = Path(o.output).expanduser() if o.output else None
    sequence_dir: Path | None = None
    if want_video and video_path is None:
        video_path = Path(default_output_name(res.steps))
    if video_path is not None and (
        video_path.suffix.lower() not in (".mp4", ".m4v")
        or str(o.output or "").endswith(("/", "\\"))
    ):
        sequence_dir = video_path  # a folder: PNG sequence is the result
        video_path = None
    have_av = render.video_status()["av"]
    if video_path is not None and not have_av:
        sequence_dir = video_path.with_name(video_path.stem + "_frames")
        out["encoder"] = "none (PyAV not installed)"
        res.warnings.append(
            "PyAV is not installed, so no MP4 was written: the frames are saved as numbered "
            f"PNG files. {render.install_help(['av']).splitlines()[1].strip()} to encode here, or run: "
            + render.ffmpeg_command(sequence_dir, video_path, res.fps, ext)
        )
        out["ffmpeg"] = render.ffmpeg_command(sequence_dir, video_path, res.fps, ext)
        video_path_wanted = video_path
        video_path = None
    else:
        video_path_wanted = video_path

    still_png: bytes | None = None
    renderer, info = _start_renderer(o, dist, (w, h), cache, manifest_path, settings, note, detail)
    with renderer as r:
        detail(f"renderer ready at {info['state']['w']}x{info['state']['h']}, grid {info['grid']}")
        st = info["state"]
        out["map_ok"] = bool(st.get("mapOk"))
        if settings.get("ground") == "realistic":
            out["terrain"] = st.get("terrain")
            if not st.get("mapOk"):
                res.warnings.append(
                    "no satellite imagery could be loaded: the ground is a plain grid"
                )
        elif not st.get("mapOk"):
            res.warnings.append(
                "the basemap tiles could not be loaded (offline?): the ground is a plain grid"
            )
        if r.frame_count() != n:
            raise RuntimeError(f"the page counts {r.frame_count()} frames but the plan has {n}")
        out["page_report"] = r.report()

        if o.still:
            t_still = o.still_at if o.still_at is not None else res.duration / 2
            still_png = r.frame_at(max(0.0, min(res.duration, t_still)))
            sp_path = Path(o.still).expanduser()
            sp_path.parent.mkdir(parents=True, exist_ok=True)
            part = sp_path.with_name(sp_path.name + ".part")
            part.write_bytes(still_png)
            part.replace(sp_path)
            out["still"] = str(sp_path)
            out["still_at_s"] = t_still

        if want_video:
            done = render.existing_frames(frames_dir, n, ext)
            todo = [i for i in range(n) if i not in done]
            out["resumed_frames"] = len(done)
            out["rendered_frames"] = len(todo)
            if done:
                note(f"resuming: {len(done)} of {n} frames are already rendered")
            checked = {"disk": False}

            def progress(k: int, total: int, elapsed: float) -> None:
                if not checked["disk"] and k >= 1:
                    checked["disk"] = True
                    first = frames_dir / render.frame_name(todo[0], ext)
                    size = first.stat().st_size
                    need = size * n
                    free = shutil.disk_usage(cache.root).free
                    out["frame_bytes_estimate"] = need
                    detail(f"frames: about {need / 1e9:.2f} GB for {n} ({free / 1e9:.0f} GB free)")
                    if need > 0.9 * free:
                        raise OSError(
                            f"the frames need about {need / 1e9:.1f} GB but only {free / 1e9:.1f} GB "
                            "is free: use --frame-format jpg, a shorter clip, a smaller --size, or "
                            "--cache-dir on a bigger disk"
                        )
                if k % max(1, total // 10) == 0 or k == total:
                    per = elapsed / k
                    note(
                        f"frame {k}/{total} ({per:.2f} s each, about {per * (total - k):.0f} s left)"
                    )

            if todo:
                try:
                    render.render_frames(r, frames_dir, todo, n, ext=ext, progress=progress)
                except BaseException as exc:
                    left = n - len(render.existing_frames(frames_dir, n, ext))
                    note(
                        f"stopped with {left} of {n} frames still to draw ({type(exc).__name__}): "
                        "run the same command again to resume"
                    )
                    raise
        out["browser_messages"] = r.messages[-10:]
        out["tiles"] = r.tiles.summary()
        out["browser"] = r.browser_version
        out["software_renderer"] = r.software

    if want_video:
        if video_path is not None:
            note(f"encoding {n} frames to {video_path.name}")
            render.encode_mp4(
                frames_dir,
                n,
                video_path,
                res.fps,
                (w, h),
                crf=o.crf,
                preset=o.preset,
                ext=ext,
                metadata=mp4_metadata(res, video_path),
            )
            out.update({"video": str(video_path), "encoder": "pyav (H.264, yuv420p)"})
            out["video_bytes"] = video_path.stat().st_size
            if not o.keep_frames:
                shutil.rmtree(frames_dir.parent, ignore_errors=True)
        else:
            assert sequence_dir is not None
            _finish_sequence(frames_dir, sequence_dir, n, ext, o.keep_frames)
            out["frames_written"] = str(sequence_dir)
            out["video"] = None
            out["video_wanted"] = str(video_path_wanted) if video_path_wanted else None
    else:
        out["video"] = None
    out["provenance"] = write_provenance(res, settings, dist, video_path or sequence_dir)


def mp4_metadata(res: AnimateResult, video: Path) -> dict[str, str]:
    """Tags written into the MP4: what it is, and where the full record is."""
    sp = res.steps
    return {
        "title": res.options.title or res.bundle.manifest["title"],
        "comment": (
            f"rxd animate {_xrd_version()}: {len(sp.steps)} time steps of {', '.join(sp.used_radars)} "
            f"({sp.steps[0].time:%Y-%m-%d %H:%M:%S} to {sp.steps[-1].time:%H:%M:%S} UTC); frames between "
            "volumes are interpolated (carried along the storm motion), not observed, and are "
            f"labelled so on screen. Parameters and data: {video.with_suffix('').name}.animate.json"
        ),
    }


def _xrd_version() -> str:
    from engine import provenance

    return provenance.software_version()


def _module_version(name: str) -> str | None:
    """Installed version of a package, or None when it is not installed."""
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        pass
    try:
        import importlib

        v = getattr(importlib.import_module(name), "__version__", None)
        return str(v) if v is not None else None
    except Exception:
        return None


def provenance(res: AnimateResult, settings: dict[str, Any], dist: Path | None) -> dict[str, Any]:
    """Everything needed to say what a clip is made of: data, versions, parameters, method."""
    import platform
    import sys
    from dataclasses import asdict

    from engine import animate_render as render

    o = res.options
    params = {k: v for k, v in asdict(o).items() if k not in ("files", "command")}
    inputs = []
    for i, st in enumerate(res.steps.steps):
        for radar, (vol, off) in sorted(st.files.items()):
            size = None
            with contextlib.suppress(OSError):
                size = vol.path.stat().st_size
            inputs.append(
                {
                    "step": i,
                    "radar": radar,
                    "volume_time": vol.time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "offset_s": off,
                    "file": vol.name,
                    "path": str(vol.path),
                    "size_bytes": size,
                    "time_from": vol.source,
                }
            )
    doc = res.to_dict()
    doc["tool"] = "rxd animate"
    doc["created_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc["command"] = o.command
    doc["versions"] = {
        "radarx_desktop_engine": _xrd_version(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "radarx": _module_version("radarx"),
        "xradar": _module_version("xradar"),
        "playwright": _module_version("playwright"),
        "av": _module_version("av"),
        "browser": res.output.get("browser"),
        "app_build": render.frontend_fingerprint(dist) if dist else None,
    }
    doc["parameters"] = params
    doc["page_settings"] = {k: v for k, v in settings.items() if k != "keys"}
    doc["input_volumes"] = inputs
    doc["method"] = {
        "gridding": f"engine.mosaic: composite of DBZH reflectivity from every radar of a step on a "
        f"{o.detail_m:g} m grid (storm search on a coarse grid), weights by range and time",
        "storm": f"strongest convective cell (column maximum >= {o.cell_dbz:g} dBZ), tracker '{o.track}'",
        "interpolation": (
            "between two observed volumes the earlier is carried forward and the later back along "
            "the storm motion (and the retrieved Doppler wind where available), then blended"
        ),
        "labels": f"a frame within {plan.OBSERVED_TOL_S:g} s of a volume is 'observed'; others are "
        "'interpolated' and say so on the screen",
        "nothing_invented": "echo is only drawn where a radar measured it; the look controls never add echo",
    }
    return doc


def write_provenance(
    res: AnimateResult, settings: dict[str, Any], dist: Path | None, target: Path | None
) -> str | None:
    """Write the provenance record next to the result (``clip.animate.json`` or ``folder/animate.json``)."""
    if target is None and res.options.still:
        target = Path(res.options.still).expanduser()
    if target is None:
        return None
    path = target / "animate.json" if target.is_dir() else target.with_suffix(".animate.json")
    doc = provenance(res, settings, dist)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    from engine.api_results import dumps

    tmp.write_text(dumps(doc))
    tmp.replace(path)
    return str(path)


def _start_renderer(
    o: AnimateOptions,
    dist: Path,
    size: tuple[int, int],
    cache: data.GridCache,
    manifest_path: Path,
    settings: dict[str, Any],
    note: Note,
    detail: Note,
):
    """Start Chrome and load the clip; if the GPU path fails, fall back to the software renderer."""
    from engine import animate_render as render

    software = o.software
    while True:
        r = render.Renderer(
            dist,
            size[0],
            size[1],
            store=cache.dir,
            manifest=manifest_path,
            tile_cache=o.tile_cache,
            offline=o.offline,
            channel=o.browser,
            software=software,
            log=detail,
        )
        try:
            return r, r.load(settings)
        except BaseException as exc:
            r.close()
            text = str(exc).lower()
            gpu = any(k in text for k in ("webgl", "gl context", "3d texture", "context lost"))
            if software or not gpu or isinstance(exc, AnimateConfigError):
                raise
            note(
                "the GPU renderer could not start in Chrome: falling back to the software renderer "
                "(much slower; --software chooses it directly)"
            )
            software = True


def _finish_sequence(frames_dir: Path, dest: Path, n: int, ext: str, keep: bool) -> None:
    from engine import animate_render as render

    if dest.exists() and not any(dest.iterdir()):
        dest.rmdir()
    if not dest.exists() and not keep:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            frames_dir.replace(dest)  # one atomic rename when on the same disk
            shutil.rmtree(frames_dir.parent, ignore_errors=True)
            return
        except OSError:
            pass
    render.copy_frames(frames_dir, dest, n, ext)
    if not keep:
        shutil.rmtree(frames_dir.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------


def format_report(res: AnimateResult, table: Callable[..., str]) -> str:
    """The terminal report: steps and offsets, the storm track, frames, the camera, what was written."""
    sp = res.steps
    m = res.bundle.manifest
    lines: list[str] = []
    fs = res.frame_summary
    span = sp.times_s[-1]
    lines.append(
        f"{m['title']}  |  {len(sp.used_radars)} radar(s)  |  {len(sp.steps)} time steps over "
        f"{span / 60:.1f} min  |  {fs['frames']} frames ({res.duration:g} s at {res.fps} fps, "
        f"{res.size[0]}x{res.size[1]})"
    )
    lines.append(
        f"  time steps  reference radar {sp.reference}, tolerance {sp.tolerance_s:.0f} s; "
        "each radar contributes its nearest volume (offset in s from the reference)"
    )
    radars = sp.used_radars
    rows = []
    for row in plan.step_table(sp):
        rows.append(
            (
                row["index"],
                row["time"][11:19],
                *[
                    (f"{row['offsets_s'][r]:+.0f}" if r in row["offsets_s"] else "-")
                    for r in radars
                ],
            )
        )
    lines.append(table(("step", "UTC", *radars), rows, "    "))
    off = plan.offset_summary(sp)
    lines.append(
        "  offsets     "
        + "; ".join(
            f"{r} mean {v['mean_abs_offset_s']:.0f} s, max {v['max_abs_offset_s']:.0f} s "
            f"({int(v['steps'])}/{len(sp.steps)} steps)"
            for r, v in off.items()
        )
    )
    if sp.skipped:
        lines.append(f"  not used    {len(sp.skipped)}:")
        for s in sp.skipped[:12]:
            lines.append(f"    {s.what}: {s.reason}")
        if len(sp.skipped) > 12:
            lines.append(f"    ... and {len(sp.skipped) - 12} more (--json lists them all)")
    storm = res.storm_summary()
    lines.append(
        f"  storm       followed with the '{storm['tracker']}' tracker: moved "
        f"{storm['displacement_km']:.1f} km toward {storm['heading_deg']:.0f} deg in "
        f"{storm['span_s'] / 60:.1f} min (mean {storm['mean_speed_ms']:.1f} m/s)"
    )
    trows = [
        (
            r["index"],
            r["time"][11:19],
            f"{r['lat']:.3f}",
            f"{r['lon']:.3f}",
            f"{r['x_km']:.1f}",
            f"{r['y_km']:.1f}",
            "-" if r["speed_ms_since_previous"] is None else f"{r['speed_ms_since_previous']:.1f}",
            f"{r['max_dbz']:.0f}",
            f"{r['area_km2']:.0f}",
            f"{r['top_km']:.1f}",
        )
        for r in storm["track"]
    ]
    lines.append(
        table(
            ("step", "UTC", "lat", "lon", "x_km", "y_km", "m/s", "max_dBZ", "area_km2", "top_km"),
            trows,
            "    ",
        )
    )
    lines.append(
        f"  frames      {fs['observed_frames']} observed ({100 * (1 - fs['interpolated_share']):.0f}%), "
        f"{fs['interpolated_frames']} interpolated ({100 * fs['interpolated_share']:.0f}%); gaps between "
        f"volumes {fs['shortest_gap_s'] / 60:.1f} to {fs['longest_gap_s'] / 60:.1f} min; time-lapse "
        f"x{plan.timelapse_of(span, res.duration):.0f}; interpolation "
        + (
            "carried by the storm motion and the observed Doppler wind"
            if m["animate"]["winds"]
            else "carried along the storm motion only (no wind retrieval)"
        )
    )
    cp = res.camera
    cam_line = f"  camera      {cp.mode} from {cp.source}, follows {100 * cp.target_lag:.0f}% of the storm's move"
    if res.camera_path:
        p = res.camera_path
        cam_line += (
            f"; {p['keys']} keys, azimuth swept {p['azimuth_swept_deg']:.0f} deg, elevation "
            f"{p['elevation_deg'][0]:.0f} to {p['elevation_deg'][1]:.0f} deg, distance "
            f"{p['distance_km'][0]:.0f} to {p['distance_km'][1]:.0f} km"
        )
    lines.append(cam_line)
    if res.look:
        lines.append("  look        " + ", ".join(f"{k}={v}" for k, v in res.look.items()))
    g = res.bundle.stats
    c = res.cache
    lines.append(
        f"  grids       {len(sp.steps)} steps at {res.options.detail_m:g} m: "
        f"{c.computed['fine']} computed, {c.hits['fine']} from the cache ({c.root})"
        + (f"; peak memory {g.peak_rss_mb:.0f} MB" if g.peak_rss_mb else "")
    )
    for w in res.warnings:
        lines.append(f"  warning: {w}")
    return "\n".join(lines)

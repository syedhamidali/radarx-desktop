"""Render the PERiLS movie: convert the bundle, plan the choreography, draw every frame with the
app's own 3-D renderer in headless Chrome, encode H.264 (+ AAC) with PyAV.

Needs a scratch Python environment OUTSIDE the repository with ``playwright``, ``av`` and ``pillow``
(``pip install playwright av pillow``), the built app (``npm run build``) and Google Chrome (used
through Playwright's ``channel="chrome"``; nothing is downloaded). The data conversion needs the
repository's engine environment (numpy, scipy, pyproj); run ``--convert-only`` there::

    python tools/perils_movie/render/render_movie.py --bundle <bundle> --work <scratch dir> --convert-only
    <clipenv>/python tools/perils_movie/render/render_movie.py --bundle <bundle> --work <scratch dir> \
        --out <movie.mp4> [--audio <audio file>] [--preview]

Frames are PNG files in ``<work>/frames`` named by frame number; existing ones are kept, so a stopped
render resumes and a preview (a few segments of the film) shares its frames with the final render.
Frame ``i`` is drawn for the clip time ``i / fps`` in one full-quality pass (deterministic: nothing
depends on the speed of the machine).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tools" / "storm_clip"))
sys.path.insert(0, str(HERE))

DEFAULT_SEGMENTS = (
    "0-5,52-58,74-80,108-114"  # the 20 s preview: establishing, tornado, ground level, top-down
)


def frame_path(frames_dir: Path, i: int) -> Path | None:
    """The file of frame ``i`` (PNG or JPEG), or None."""
    for ext in ("png", "jpg"):
        f = frames_dir / f"frame_{i:05d}.{ext}"
        if f.exists():
            return f
    return None


def parse_segments(spec: str, fps: int, duration: float) -> list[int]:
    frames: list[int] = []
    for part in spec.split(","):
        a, b = (float(x) for x in part.split("-"))
        frames.extend(range(round(a * fps), min(round(b * fps), int(duration * fps))))
    return frames


def build_tornado_seed(
    bundle: Path, clip: Path, work: Path, log=print, manifest_name: str = "manifest.json"
) -> bool:
    """Write ``tornado_seed.json`` (tools/tornado_sim/make_seed.py) for the data span of the tornado shot.

    The seed builder reads ``<movie>/bundle`` and ``<movie>/events.json``; a scratch folder with a link to the bundle and the
    events of this plan stands in for the movie folder. Without it (no bundle of that layout, an error) the film keeps the
    particle funnel and says so in the log."""
    import subprocess

    ev = json.loads((clip / "events.json").read_text())
    shot = (ev.get("tornado") or {}).get("shot")
    focus = (json.loads((clip / "timeline.json").read_text()).get("focus") or {}).get(
        "sim_span_data_s"
    )
    if focus:  # the tornado focus: the simulation runs over the span of its close angles
        shot = {"t0_data_s": focus[0], "t1_data_s": focus[1]}
    maker = Path(__file__).resolve().parents[2] / "tornado_sim" / "make_seed.py"
    if not shot or not maker.exists() or not (bundle / "tornadoes" / "tornadoes.json").exists():
        log(
            "no tornado seed (no tornado shot, or no survey in the bundle): the particle funnel is used"
        )
        return False
    src = work / "seed_src"
    src.mkdir(parents=True, exist_ok=True)
    link = src / "bundle"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(bundle.resolve())
    (src / "events.json").write_bytes((clip / "events.json").read_bytes())
    cmd = [
        sys.executable,
        str(maker),
        "--movie",
        str(src),
        "--out",
        str(clip / "tornado_seed.json"),
        "--shot",
        f"{shot['t0_data_s']},{shot['t1_data_s']}",
        "--echo-series",
        "--echo-half-km",
        "9",
        "--manifest",
        manifest_name,
    ]
    if focus:  # a funnel that forms in 8 s, not 25 s: the first angle sees the surveyed begin time and 15 s after it
        cmd += ["--ramp-form", "8"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    log(
        (r.stdout or r.stderr).strip().splitlines()[-1]
        if (r.stdout or r.stderr)
        else "seed written"
    )
    return r.returncode == 0


def prepare_bundle(
    bundle: Path,
    work: Path,
    export_dir: Path | None,
    counties: Path | None = None,
    log=print,
    sim: bool = True,
    plan: str = "film",
    keep_plan: bool = False,
    manifest_name: str = "manifest.json",
) -> Path:
    """Convert the bundle (if needed) and write timeline / camera / events next to the converted files."""
    import choreography
    import convert_bundle

    clip = work / "clip_bundle"
    stamp = clip / "perils.json"
    man = bundle / manifest_name
    if not stamp.exists() or stamp.stat().st_mtime < man.stat().st_mtime:
        log(f"converting {bundle} -> {clip}")
        convert_bundle.convert(bundle, clip, log=log, manifest_name=manifest_name)
    else:
        log(f"conversion up to date: {clip}")
    if not (clip / "ground.png").exists():
        try:
            import make_ground

            p = json.loads(stamp.read_text())
            half = 260.0
            for m in p.get("mrms", []):
                if m:
                    half = max(half, abs(m["x0"]) + 10)
            meta = make_ground.draw(
                clip / "ground.png",
                convert_bundle.Geo(p["origin"]["lat"], p["origin"]["lon"]),
                half,
                0.0,
                0.0,
                Path.home() / ".local/share/cartopy/shapefiles/natural_earth",
                counties,
                log=log,
            )
            p["ground"] = meta
            stamp.write_text(json.dumps(p))
        except Exception as e:  # the movie still renders: a plain grid ground, said on screen
            log(f"no ground map ({e}); the ground will be a plain grid")
    if (
        keep_plan and (clip / "timeline.json").exists()
    ):  # experiments: the plan on disk is used as it is
        log("keeping the plan on disk (--keep-plan)")
    elif plan == "v2":  # the v2 film (a single tornado shot), kept reproducible
        choreography.write(stamp, clip, 120.0, 30, log=log)
    else:  # the tornado focus: "film" (perils_qlcs_movie_v3.mp4) or "clip" (perils_tornado_focus.mp4)
        import focus_plan

        focus_plan.write_focus(stamp, clip, plan, 30, log=log)
    if sim:
        build_tornado_seed(bundle, clip, work, log=log, manifest_name=manifest_name)
    if export_dir:
        export_dir.mkdir(parents=True, exist_ok=True)
        for name in ("timeline.json", "camera_path.json", "events.json"):
            (export_dir / name).write_bytes((clip / name).read_bytes())
        log(f"timeline.json, camera_path.json, events.json -> {export_dir}")
    return clip


async def draw_frames(a, clip: Path, frames: list[int], frames_dir: Path, log=print) -> None:
    from playwright.async_api import async_playwright
    from render_clip import serve
    from tilecache import TileCache

    dist = Path(a.app_dist).expanduser().resolve()
    if not (dist / "index.html").exists():
        sys.exit(f"{dist}/index.html not found: run `npm run build` first (or pass --app-dist)")
    todo = [i for i in frames if a.redraw or a.no_shot or frame_path(frames_dir, i) is None]
    log(
        f"{len(frames)} frames wanted, {len(frames) - len(todo)} already on disk, {len(todo)} to draw"
    )
    if not todo:
        return
    srv, port = serve(dist, clip, TileCache(None, offline=True))
    settings = {"vexag": 2.4, "stormStyle": a.style, "palette": a.palette, "light": a.light}
    if a.look_preset:
        settings["lookPreset"] = a.look_preset
    # the film's own look (stylised, said on screen): the stratiform shield below 24 dBZ is not drawn as cloud
    # (the MRMS ground layer and the 3-D volume above it still show it where it matters), strong echo stands out
    settings["lookOverrides"] = {"lowDbzCutoff": 24.0, "rampMidDbz": 42.0, "densityScale": 1.5}
    for item in a.set or []:
        k, v = item.split("=", 1)
        try:
            settings["lookOverrides"][k.strip()] = float(v)
        except ValueError:
            settings["lookOverrides"][k.strip()] = v.strip()
    perils = {
        "cloudBaseKm": a.cloud_base_km,
        "fov": a.fov,
        "sim": not a.no_sim,
        "simScale": a.sim_scale,
    }
    if a.title:
        perils["title"] = a.title
    if a.subtitle:
        perils["subtitle"] = a.subtitle
    if a.layers:
        perils["layers"] = {k: float(v) for k, v in (p.split("=") for p in a.layers.split(","))}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",
            headless=True,
            args=[
                "--no-proxy-server",
                "--use-gl=angle",
                "--use-angle=metal",
                "--ignore-gpu-blocklist",
                "--enable-gpu-rasterization",
            ],
        )
        page = await browser.new_page(
            viewport={"width": a.width, "height": a.height}, device_scale_factor=1
        )
        logs: list[str] = []
        page.on(
            "console",
            lambda m: (
                logs.append(f"{m.type}: {m.text[:300]}") if m.type in ("error", "warning") else None
            ),
        )
        page.on("pageerror", lambda e: logs.append(f"PAGEERROR: {str(e)[:300]}"))
        await page.goto(f"http://127.0.0.1:{port}/?clip=1")
        await page.wait_for_function("() => !!window.__xrClip", timeout=30000)
        try:
            info = await page.evaluate(
                "(o) => window.__xrClip.loadPerils('/bundle', o)",
                {"settings": settings, "perils": perils},
            )
        except Exception as exc:
            msg = str(exc).split("\n")[0]
            sys.exit(f"error: {msg}\n" + "\n".join(logs[-5:]))
        log(f"loaded {info['steps']} steps, grid {info['grid']}")
        await page.evaluate("() => window.__xrClip.prepare('osm')")
        st = await page.evaluate("() => window.__xrClip.info()")
        if st["fatal"]:
            sys.exit(f"renderer error: {st['fatal']}")
        log(f"renderer ready at {st['w']}x{st['h']}")
        t0 = time.time()
        cams: dict = {}
        lays: dict = {}
        for n, i in enumerate(todo):
            await page.evaluate("(i) => window.__xrClip.renderFrame(i)", i)
            if a.cam_dump:
                cams[i] = await page.evaluate("() => window.__xrPerilsCam || null")
            if a.layout_dump:
                # what the frame's overlay drew and where (cards and labels), and the layout rules it broke: perilsLayout.ts
                lays[i] = await page.evaluate("() => window.__xrPerilsLayout || null")
            if a.no_shot:
                continue
            ext = "jpg" if a.frame_format == "jpeg" else "png"
            shot = (
                await page.screenshot(type="jpeg", quality=a.jpeg_quality)
                if ext == "jpg"
                else await page.screenshot(type="png")
            )
            (frames_dir / f"frame_{i:05d}.{ext}.part").write_bytes(shot)
            (frames_dir / f"frame_{i:05d}.{ext}.part").rename(frames_dir / f"frame_{i:05d}.{ext}")
            if n % 30 == 0 or n == len(todo) - 1:
                el = time.time() - t0
                log(
                    f"frame {i} ({n + 1}/{len(todo)})  {el:.0f}s elapsed, {el / (n + 1):.2f}s per frame, ETA {(len(todo) - n - 1) * el / (n + 1):.0f}s"
                )
        if a.cam_dump:
            # the camera of each drawn frame as the scene saw it (merged with earlier dumps), for measure_rotation.py
            dump = Path(a.cam_dump)
            old = json.loads(dump.read_text()) if dump.exists() else {}
            old.update({str(k): v for k, v in cams.items()})
            dump.write_text(json.dumps(old))
        if a.layout_dump:
            dump = Path(a.layout_dump)
            old = json.loads(dump.read_text()) if dump.exists() else {}
            old.update({str(k): v for k, v in lays.items()})
            dump.write_text(json.dumps(old))
        rep = await page.evaluate("() => window.__xrClip.report()")
        (frames_dir.parent / "render_report.json").write_text(json.dumps(rep, indent=1))
        await browser.close()
    srv.shutdown()
    if logs:
        log("browser messages:\n  " + "\n  ".join(sorted(set(logs))[-12:]))


def read_audio(path: Path, sr: int = 48000):
    """Any audio file PyAV reads -> float32 (2, n) at ``sr``, stereo; padded with silence if the file is shorter than 130 s."""
    import av
    import numpy as np

    src = av.open(str(path))
    ain = src.streams.audio[0]
    rs = av.AudioResampler(format="fltp", layout="stereo", rate=sr)
    parts = []
    for fr in src.decode(ain):
        for r in rs.resample(fr):
            parts.append(r.to_ndarray())
    for r in rs.resample(None):
        parts.append(r.to_ndarray())
    src.close()
    x = np.concatenate(parts, axis=1) if parts else np.zeros((2, 0), np.float32)
    return (
        np.concatenate([x, np.zeros((2, int(130 * sr) - x.shape[1]), np.float32)], axis=1)
        if x.shape[1] < 130 * sr
        else x
    )


def encode(
    frames: list[int],
    frames_dir: Path,
    out: Path,
    fps: int,
    audio: Path | None,
    crf: int,
    log=print,
) -> None:
    """H.264 (yuv420p, +faststart) from the frame files; optional AAC track from any audio file PyAV can read."""
    from fractions import Fraction

    import av
    import numpy as np
    from PIL import Image

    out.parent.mkdir(parents=True, exist_ok=True)
    first = Image.open(frame_path(frames_dir, frames[0]))
    w, h = first.size
    container = av.open(str(out), "w", options={"movflags": "+faststart"})
    vs = container.add_stream("libx264", rate=fps)
    vs.width, vs.height = w - w % 2, h - h % 2
    vs.pix_fmt = "yuv420p"
    vs.options = {"crf": str(crf), "preset": "medium"}
    aud_packets: list = []
    if audio:
        sr = 48000
        samples = read_audio(audio, sr)
        per = sr / fps  # samples per frame (1600 at 48 kHz and 30 fps)
        # the audio of exactly the frames that are encoded: a preview of a few segments gets the sound of the same
        # segments, the full film its whole track
        pieces = [samples[:, round(i * per) : round((i + 1) * per)] for i in frames]
        planar = np.ascontiguousarray(np.concatenate(pieces, axis=1), dtype=np.float32)
        as_ = container.add_stream("aac", rate=sr)
        as_.layout = "stereo"
        as_.bit_rate = 192_000
        for i0 in range(0, planar.shape[1], 1024):
            chunk = planar[:, i0 : i0 + 1024]
            af = av.AudioFrame.from_ndarray(
                np.ascontiguousarray(chunk), format="fltp", layout="stereo"
            )
            af.sample_rate = sr
            af.pts = i0
            af.time_base = Fraction(1, sr)
            aud_packets.extend(as_.encode(af))
        aud_packets.extend(as_.encode())
        log(f"audio: {planar.shape[1] / sr:.2f} s from {audio.name} ({len(frames)} frames)")
    for k, i in enumerate(frames):
        fp = frame_path(frames_dir, i)
        if fp is None:
            raise SystemExit(f"frame {i} is missing in {frames_dir}")
        img = Image.open(fp).convert("RGB")
        if img.size != (vs.width, vs.height):
            img = img.crop((0, 0, vs.width, vs.height))
        vf = av.VideoFrame.from_image(img)
        for pkt in vs.encode(vf):
            container.mux(pkt)
        while aud_packets and aud_packets[0].pts * aud_packets[0].time_base <= (k + 1) / fps:
            container.mux(aud_packets.pop(0))
        if k % 300 == 0:
            log(f"encoded {k}/{len(frames)}")
    for pkt in vs.encode():
        container.mux(pkt)
    for pkt in aud_packets:
        container.mux(pkt)
    container.close()
    log(
        f"wrote {out}  ({len(frames)} frames, {len(frames) / fps:.2f} s, {out.stat().st_size / 1e6:.1f} MB)"
    )


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--bundle", required=True, help="the data bundle (SPEC v1, manifest.json inside)"
    )
    ap.add_argument("--work", required=True, help="scratch folder: converted bundle, frames")
    ap.add_argument("--out", default=None, help="MP4 to write")
    ap.add_argument(
        "--redraw",
        action="store_true",
        help="draw the wanted frames again even if a file exists (stills while the look is being tuned)",
    )
    ap.add_argument(
        "--manifest",
        default="manifest.json",
        help="the bundle's manifest: manifest.json (reflectivity merge v1) or manifest_v2.json (calibrated, attenuation-corrected, with quality)",
    )
    ap.add_argument(
        "--keep-plan",
        action="store_true",
        help="do not plan again: use the timeline / camera / events already in the work folder (experiments)",
    )
    ap.add_argument(
        "--plan",
        default="film",
        choices=["film", "clip", "v2"],
        help="film: the 120 s film with the tornado focus (v3); clip: the stand-alone tornado-focus clip; v2: the earlier film",
    )
    ap.add_argument("--audio", default=None, help="an audio file to mux (any format PyAV reads)")
    ap.add_argument(
        "--export-dir",
        default=None,
        help="also write timeline.json, camera_path.json, events.json here",
    )
    ap.add_argument(
        "--counties",
        default=None,
        help="US Census county lines (.shp) for the ground map (optional)",
    )
    ap.add_argument(
        "--convert-only",
        action="store_true",
        help="convert + plan, then stop (run in the engine environment)",
    )
    ap.add_argument(
        "--preview",
        nargs="?",
        const=DEFAULT_SEGMENTS,
        default=None,
        help="render only these clip-time segments, e.g. 0-5,52-58 (default: 20 s of the film)",
    )
    ap.add_argument("--frames", default=None, help="a:b frame range (tests)")
    ap.add_argument(
        "--at",
        default=None,
        help="render the frames at these clip times (s), comma separated (stills)",
    )
    ap.add_argument(
        "--no-render", action="store_true", help="do not draw, only encode the frames on disk"
    )
    ap.add_argument("--app-dist", default=str(REPO / "dist"))
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument(
        "--frame-format",
        default="jpeg",
        choices=["jpeg", "png"],
        help="frame files (JPEG q95 by default: 8.5 GB of PNG do not fit next to everything else; the video is 4:2:0 H.264 anyway)",
    )
    ap.add_argument("--jpeg-quality", type=int, default=95)
    ap.add_argument(
        "--no-sim",
        action="store_true",
        help="draw the particle funnel instead of the tornado simulation (tools/tornado_sim) in the tornado shot",
    )
    ap.add_argument(
        "--sim-scale",
        type=float,
        default=1.0,
        help="internal render scale of the simulation (drafts)",
    )
    ap.add_argument(
        "--cam-dump",
        default=None,
        help="write the camera of every drawn frame (JSON) for measure_rotation.py",
    )
    ap.add_argument(
        "--layout-dump",
        default=None,
        help="write the layout of every drawn frame (JSON): the items (cards, labels) with their boxes and the layout rules broken",
    )
    ap.add_argument(
        "--no-shot",
        action="store_true",
        help="draw the frames but save no picture (with --layout-dump: a layout check)",
    )
    ap.add_argument(
        "--frame-step",
        type=int,
        default=1,
        help="with --frames or the whole film: every n-th frame only (a layout check)",
    )
    ap.add_argument("--crf", type=int, default=17)
    ap.add_argument(
        "--style",
        default="realistic",
        choices=["realistic", "scientific"],
        help="the stylised storm look (default) or the dBZ colour scale",
    )
    ap.add_argument("--palette", default="stormy", choices=["white", "soft", "stormy", "cyclone"])
    ap.add_argument(
        "--light", default="overcast", choices=["daylight", "dusk", "overcast", "night"]
    )
    ap.add_argument("--look-preset", default=None)
    ap.add_argument("--set", action="append", default=None, metavar="KEY=VALUE")
    ap.add_argument(
        "--title", default="PERiLS IOP2: squall line and EF2 tornado", help="title at the top left"
    )
    ap.add_argument(
        "--subtitle",
        default="Mississippi / Alabama, 30 March 2022 local evening · real radar, lightning, surface and sounding data",
    )
    ap.add_argument("--cloud-base-km", type=float, default=1.0)
    ap.add_argument("--fov", type=float, default=0.0)
    ap.add_argument("--layers", default=None, help="layer switches, e.g. hail=0,flow=1")
    a = ap.parse_args(argv)

    bundle = Path(a.bundle).expanduser().resolve()
    work = Path(a.work).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    export = Path(a.export_dir).expanduser().resolve() if a.export_dir else None
    clip = prepare_bundle(
        bundle,
        work,
        export,
        Path(a.counties).expanduser() if a.counties else None,
        sim=not a.no_sim,
        plan=a.plan,
        keep_plan=a.keep_plan,
        manifest_name=a.manifest,
    )
    if a.convert_only:
        return
    tl = json.loads((clip / "timeline.json").read_text())
    fps, duration = tl["fps"], tl["clip_duration_s"]
    n_all = tl["frames"]
    if a.at:
        frames = [min(n_all - 1, round(float(t) * fps)) for t in a.at.split(",")]
    elif a.frames:
        lo, hi = (int(v) for v in a.frames.split(":"))
        frames = list(range(lo, min(hi, n_all)))
    elif a.preview:
        frames = parse_segments(a.preview, fps, duration)
    else:
        frames = list(range(n_all))
    if a.frame_step > 1:
        frames = frames[:: a.frame_step]
    frames_dir = work / "frames"
    frames_dir.mkdir(exist_ok=True)
    if not a.no_render:
        asyncio.run(draw_frames(a, clip, frames, frames_dir))
    if a.out:
        encode(
            frames,
            frames_dir,
            Path(a.out).expanduser().resolve(),
            fps,
            Path(a.audio).expanduser().resolve() if a.audio else None,
            a.crf,
        )


if __name__ == "__main__":
    main()

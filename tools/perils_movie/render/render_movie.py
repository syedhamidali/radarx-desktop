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


def parse_segments(spec: str, fps: int, duration: float) -> list[int]:
    frames: list[int] = []
    for part in spec.split(","):
        a, b = (float(x) for x in part.split("-"))
        frames.extend(range(round(a * fps), min(round(b * fps), int(duration * fps))))
    return frames


def prepare_bundle(
    bundle: Path, work: Path, export_dir: Path | None, counties: Path | None = None, log=print
) -> Path:
    """Convert the bundle (if needed) and write timeline / camera / events next to the converted files."""
    import choreography
    import convert_bundle

    clip = work / "clip_bundle"
    stamp = clip / "perils.json"
    man = bundle / "manifest.json"
    if not stamp.exists() or stamp.stat().st_mtime < man.stat().st_mtime:
        log(f"converting {bundle} -> {clip}")
        convert_bundle.convert(bundle, clip, log=log)
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
    choreography.write(stamp, clip, 120.0, 30, log=log)
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
    todo = [i for i in frames if not (frames_dir / f"frame_{i:05d}.png").exists()]
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
    perils = {"cloudBaseKm": a.cloud_base_km, "fov": a.fov}
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
        for n, i in enumerate(todo):
            await page.evaluate("(i) => window.__xrClip.renderFrame(i)", i)
            (frames_dir / f"frame_{i:05d}.png.part").write_bytes(await page.screenshot(type="png"))
            (frames_dir / f"frame_{i:05d}.png.part").rename(frames_dir / f"frame_{i:05d}.png")
            if n % 30 == 0 or n == len(todo) - 1:
                el = time.time() - t0
                log(
                    f"frame {i} ({n + 1}/{len(todo)})  {el:.0f}s elapsed, {el / (n + 1):.2f}s per frame, ETA {(len(todo) - n - 1) * el / (n + 1):.0f}s"
                )
        rep = await page.evaluate("() => window.__xrClip.report()")
        (frames_dir.parent / "render_report.json").write_text(json.dumps(rep, indent=1))
        await browser.close()
    srv.shutdown()
    if logs:
        log("browser messages:\n  " + "\n  ".join(sorted(set(logs))[-12:]))


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
    first = Image.open(frames_dir / f"frame_{frames[0]:05d}.png")
    w, h = first.size
    container = av.open(str(out), "w", options={"movflags": "+faststart"})
    vs = container.add_stream("libx264", rate=fps)
    vs.width, vs.height = w - w % 2, h - h % 2
    vs.pix_fmt = "yuv420p"
    vs.options = {"crf": str(crf), "preset": "medium"}
    aud_packets: list = []
    if audio:
        src = av.open(str(audio))
        ain = src.streams.audio[0]
        sr = 48000
        as_ = container.add_stream("aac", rate=sr)
        as_.layout = "stereo"
        as_.bit_rate = 192_000
        rs = av.AudioResampler(format="fltp", layout="stereo", rate=sr)
        pts = 0
        limit = int(len(frames) / fps * sr)
        for fr in src.decode(ain):
            for r in rs.resample(fr):
                n = r.samples
                if pts >= limit:
                    break
                arr = r.to_ndarray()
                if pts + n > limit:
                    arr = arr[:, : limit - pts]
                    n = arr.shape[1]
                af = av.AudioFrame.from_ndarray(
                    np.ascontiguousarray(arr), format="fltp", layout="stereo"
                )
                af.sample_rate = sr
                af.pts = pts
                af.time_base = Fraction(1, sr)
                aud_packets.extend(as_.encode(af))
                pts += n
        aud_packets.extend(as_.encode())
        src.close()
        log(f"audio: {pts / sr:.2f} s from {audio.name}")
    for k, i in enumerate(frames):
        img = Image.open(frames_dir / f"frame_{i:05d}.png").convert("RGB")
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
    ap.add_argument("--cloud-base-km", type=float, default=1.0)
    ap.add_argument("--fov", type=float, default=0.0)
    ap.add_argument("--layers", default=None, help="layer switches, e.g. hail=0,flow=1")
    a = ap.parse_args(argv)

    bundle = Path(a.bundle).expanduser().resolve()
    work = Path(a.work).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    export = Path(a.export_dir).expanduser().resolve() if a.export_dir else None
    clip = prepare_bundle(
        bundle, work, export, Path(a.counties).expanduser() if a.counties else None
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

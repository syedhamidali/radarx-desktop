"""Render a storm-centred 3D clip (H.264 MP4) with the app's own 3D viewer in headless Chrome.

Needs a scratch Python environment OUTSIDE the repository with ``playwright``, ``av`` (PyAV) and
``pillow``; Chrome is used through Playwright's ``channel="chrome"`` (no browser download); nothing
is added to the repository's dependencies. See ``docs/STORM_CLIP.md``::

    python -m venv /tmp/clipenv && /tmp/clipenv/bin/pip install playwright av pillow
    npm run build                                   # the app (dist/) is served by this script
    python/.venv/bin/python tools/storm_clip/clipdata.py --case <case dir> --out /tmp/bundle
    /tmp/clipenv/bin/python tools/storm_clip/render_clip.py --bundle /tmp/bundle \
        --out ~/Downloads/xradar-exports/storm3d.mp4

Frames are drawn deterministically: the page renders frame i for the clip time i / fps (no real
time clock), the script grabs the pixels and PyAV encodes them (yuv420p, +faststart).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import io
import json
import socketserver
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tilecache import TileCache


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the built app at /, the data bundle at /bundle/ and cached map tiles at /tiles/."""

    def __init__(self, *args, dist: Path, bundle: Path, tiles: TileCache, **kw):
        self._dist, self._bundle, self._tiles = dist, bundle, tiles
        super().__init__(*args, **kw)

    def do_GET(self) -> None:
        if self.path.startswith("/tiles/"):
            req = self._tiles.parse(self.path)
            got = self._tiles.get(*req) if req else None
            if got is None:
                self.send_error(404, "tile not available")
                return
            data, ctype = got
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/bundle/"):
            return str(self._bundle / path[len("/bundle/") :].lstrip("/"))
        return str(self._dist / path.lstrip("/"))

    def log_message(self, *args) -> None:  # quiet
        pass


def serve(
    dist: Path, bundle: Path, tiles: TileCache | None = None
) -> tuple[socketserver.TCPServer, int]:
    handler = functools.partial(_Handler, dist=dist, bundle=bundle, tiles=tiles or TileCache())
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def render(a: argparse.Namespace) -> None:
    import av
    from PIL import Image
    from playwright.async_api import async_playwright

    dist = Path(a.app_dist).expanduser().resolve()
    bundle = Path(a.bundle).expanduser().resolve()
    if not (dist / "index.html").exists():
        sys.exit(f"{dist}/index.html not found: run `npm run build` first (or pass --app-dist)")
    if not (bundle / "manifest.json").exists():
        sys.exit(f"{bundle}/manifest.json not found: run tools/storm_clip/clipdata.py first")
    out = Path(a.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tiles = TileCache(a.tile_cache, offline=a.offline)
    srv, port = serve(dist, bundle, tiles)
    W, H, fps = a.width, a.height, a.fps
    manifest = json.loads((bundle / "manifest.json").read_text())
    span = manifest["steps"][-1]["t_s"] - manifest["steps"][0]["t_s"]
    duration = a.duration
    if (
        a.timelapse
    ):  # clip length from the time-lapse factor: the data span / factor + the 1.5 s holds
        duration = round(span / a.timelapse + 3.0, 1)
        print(f"time-lapse x{a.timelapse:g}: {span / 60:.1f} min of data in {duration:g} s")
    settings = {"fps": fps, "duration": duration}
    if a.vexag is not None:
        settings["vexag"] = a.vexag
    else:
        settings["vexag"] = 1.0 if a.camera == "observer" else 4.0
    settings["camera"] = a.camera
    if a.palette:
        settings["palette"] = a.palette
    settings["light"] = a.light
    settings["preset"] = a.orbit
    if a.view_km:
        settings["viewKm"] = a.view_km
    obs: dict = {}
    if a.observer:
        ox, oy = (float(v) for v in a.observer.split(","))
        obs.update(x=ox, y=oy)
    for key, arg in (
        ("distKm", a.observer_dist),
        ("upwindKm", a.observer_upwind),
        ("eyeM", a.eye_m),
        ("pan", a.pan),
        ("pitchDeg", a.pitch),
    ):
        if arg is not None:
            obs[key] = arg
    if a.observer_side:
        obs["side"] = a.observer_side
    if obs:
        settings["observer"] = obs
    if a.sun_azimuth is not None or a.sun_elevation is not None:
        settings["sun"] = {
            "azimuthDeg": a.sun_azimuth if a.sun_azimuth is not None else 205.0,
            "elevationDeg": a.sun_elevation if a.sun_elevation is not None else 13.0,
        }
    if a.sea:
        settings["sea"] = True
    if a.cloud_freq:
        hf, vf = (float(v) for v in a.cloud_freq.split(","))
        settings["cloudFreq"] = [hf, vf]
    if a.look_preset:
        settings["lookPreset"] = a.look_preset
    if a.look:
        lp = Path(a.look).expanduser()
        settings["lookText"] = lp.read_text()
        settings["lookFormat"] = "toml" if lp.suffix.lower() == ".toml" else "json"
    overrides: dict = {}
    for item in a.set or []:
        if "=" not in item:
            sys.exit(f"--set expects key=value, got '{item}'")
        k, v = item.split("=", 1)
        try:
            overrides[k.strip()] = float(v)
        except ValueError:
            overrides[k.strip()] = v.strip()
    if overrides:
        settings["lookOverrides"] = overrides
    settings["flow"] = not a.no_flow
    settings["lightning"] = "off" if a.no_lightning else "auto"
    settings["lightningSeed"] = a.lightning_seed
    settings["lightningRate"] = a.lightning_rate
    settings["t10Km"] = a.t10_km
    settings["soundCompress"] = a.sound_compress
    with_sound = a.camera in ("observer", "flight") and not a.no_sound
    settings["audio"] = with_sound
    ground = "realistic" if a.realistic else a.ground
    storm_style = "realistic" if a.realistic else a.storm_style
    settings["ground"] = ground
    settings["stormStyle"] = storm_style
    settings["tileBase"] = "/tiles"
    for key in ("title", "subtitle"):
        if getattr(a, key):
            settings[key] = getattr(a, key)
    settings["mapTheme"] = a.map_theme
    still_times = [float(x) for x in a.stills_at.split(",")] if a.stills_at else []
    still_frames = {round(t * fps): t for t in still_times}
    stills_dir = Path(a.stills).expanduser().resolve() if a.stills else None
    if stills_dir:
        stills_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",
            headless=True,
            args=[
                "--use-gl=angle",
                "--use-angle=metal",
                "--ignore-gpu-blocklist",
                "--enable-gpu-rasterization",
            ],
        )
        page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
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
                "(a) => window.__xrClip.load(a[0], a[1])", ["/bundle", settings]
            )
        except Exception as exc:  # a bad look file: say which key, no stack trace
            msg = str(exc).split("\n")[0]
            sys.exit(f"error: {msg.replace('Page.evaluate: ', '')}")
        print(f"loaded {info['steps']} time steps, grid {info['grid']}, '{info['title']}'")
        await page.evaluate("(p) => window.__xrClip.prepare(p)", a.basemap)
        st = await page.evaluate("() => window.__xrClip.info()")
        if st["fatal"]:
            sys.exit(f"renderer error: {st['fatal']}")
        print(
            f"renderer ready at {st['w']}x{st['h']}, map {'ok' if st['mapOk'] else 'MISSING (grid ground)'}"
        )
        if ground == "realistic":
            print(f"realistic ground: {st.get('terrain') or 'NO TERRAIN (flat ground)'}")
            if not st["mapOk"]:
                print("WARNING: no satellite imagery could be loaded: the ground is a plain grid")
        print(tiles.summary())
        n = await page.evaluate("() => window.__xrClip.frameCount()")
        if a.max_frames:
            n = min(n, a.max_frames)
        rep_info = await page.evaluate("() => window.__xrClip.report()")
        print("report:", json.dumps(rep_info))
        audio = None
        if with_sound:
            print("rendering the soundscape (offline Web Audio) ...")
            audio = await page.evaluate("(sr) => window.__xrClip.renderAudio(sr)", 48000)
            print(
                f"audio: {audio['frames'] / audio['sampleRate']:.1f} s, peak {audio['peak']:.3f}, "
                f"wind {audio['windMs']['min']:.1f}-{audio['windMs']['max']:.1f} m/s ({audio['windSource']})"
            )
            report = {k: v for k, v in audio.items() if k != "pcm"}
            out.with_suffix(".audio.json").write_text(json.dumps(report, indent=1))
            for th in audio["thunders"]:
                print(
                    f"  flash {th['flash']:3d} ({th['kind']}, {th['source']}) at {th['tFlash']:6.2f} s: "
                    f"{th['distKm']:5.1f} km -> thunder arrives {th['arrival']:6.2f} s (delay {th['delay']:.2f} s)"
                )

        container = av.open(str(out), "w", options={"movflags": "+faststart"})
        stream = container.add_stream("libx264", rate=fps)
        stream.width, stream.height = W - W % 2, H - H % 2
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": str(a.crf), "preset": a.preset}
        audio_packets: list = []
        if audio:
            import base64
            from fractions import Fraction

            import numpy as np

            sr = audio["sampleRate"]
            pcm = np.frombuffer(base64.b64decode(audio["pcm"]), dtype="<i2").reshape(-1, 2)
            astream = container.add_stream("aac", rate=sr)
            astream.layout = "stereo"
            astream.bit_rate = 160_000
            planar = np.ascontiguousarray((pcm.astype(np.float32) / 32768.0).T)
            for i0 in range(0, planar.shape[1], 1024):
                chunk = planar[:, i0 : i0 + 1024]
                af = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="stereo")
                af.sample_rate = sr
                af.pts = i0
                af.time_base = Fraction(1, sr)
                audio_packets.extend(astream.encode(af))
            audio_packets.extend(astream.encode())
        t0 = time.time()
        for i in range(0, n, a.every):
            await page.evaluate("(i) => window.__xrClip.renderFrame(i)", i)
            png = await page.screenshot(type="png")
            img = Image.open(io.BytesIO(png)).convert("RGB")
            if img.size != (stream.width, stream.height):
                img = img.crop((0, 0, stream.width, stream.height))
            if stills_dir and i in still_frames:
                img.save(stills_dir / f"still_{still_frames[i]:05.1f}s.png")
            frame = av.VideoFrame.from_image(img)
            for pkt in stream.encode(frame):
                container.mux(pkt)
            # the audio up to this frame's time goes in with it (a well interleaved file)
            while (
                audio_packets and audio_packets[0].pts * audio_packets[0].time_base <= (i + 1) / fps
            ):
                container.mux(audio_packets.pop(0))
            if i % 30 == 0 or i == n - 1:
                el = time.time() - t0
                print(
                    f"frame {i + 1}/{n}  {el:.0f}s elapsed, {el / (i // a.every + 1):.2f}s per frame, ETA {(n - i - 1) / a.every * el / (i // a.every + 1):.0f}s",
                    flush=True,
                )
        for pkt in stream.encode():
            container.mux(pkt)
        for pkt in audio_packets:
            container.mux(pkt)
        container.close()
        await browser.close()
    srv.shutdown()
    if logs:
        print("browser messages:\n  " + "\n  ".join(logs[-10:]))
    print(tiles.summary())
    size = out.stat().st_size / 1e6
    print(f"wrote {out}  ({n} frames, {n / fps:.1f} s at {fps} fps, {W}x{H}, {size:.1f} MB)")
    (out.with_suffix(".json")).write_text(
        json.dumps(
            {"frames": n, "fps": fps, "width": W, "height": H, "bundle": str(bundle)}, indent=1
        )
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bundle", required=True, help="data bundle written by clipdata.py")
    ap.add_argument("--out", required=True, help="MP4 file to write")
    ap.add_argument("--app-dist", default=str(REPO / "dist"), help="the built app (npm run build)")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--duration", type=float, default=36.0, help="clip length in seconds")
    ap.add_argument(
        "--vexag",
        type=float,
        default=None,
        help="vertical exaggeration of the scene (default 4 for the orbit camera, 1 = true scale for the observer)",
    )
    ap.add_argument(
        "--camera",
        default="orbit",
        choices=["orbit", "observer", "flight"],
        help="the cinematic orbit (default), a fixed camera on the ground like one on a window sill, "
        "or the cyclone flight (in along a rainband, through the eyewall, into the eye)",
    )
    ap.add_argument(
        "--palette",
        default=None,
        choices=["white", "soft", "stormy", "cyclone"],
        help="palette of the realistic storm look (default white)",
    )
    ap.add_argument(
        "--light",
        default="daylight",
        choices=["daylight", "dusk", "overcast", "night"],
        help="realistic look: a stylised daylight (default; labelled), the dusk light, a thick overcast "
        "(dim stylised light) or night (no sun, lit by lightning)",
    )
    ap.add_argument(
        "--orbit",
        default="storm",
        choices=["storm", "cyclone"],
        help="orbit choreography: one thunderstorm (default) or a wide system such as a tropical cyclone",
    )
    ap.add_argument(
        "--view-km",
        type=float,
        default=None,
        help="size of the scene for --orbit cyclone (default 330)",
    )
    ap.add_argument(
        "--observer", default=None, help="observer position 'x,y' in km of the bundle's scene frame"
    )
    ap.add_argument(
        "--observer-dist", type=float, default=None, help="km from the storm track (default 10)"
    )
    ap.add_argument(
        "--observer-upwind",
        type=float,
        default=None,
        help="km upwind of the track's middle (default 4)",
    )
    ap.add_argument(
        "--observer-side", default=None, choices=["left", "right"], help="side of the track"
    )
    ap.add_argument(
        "--eye-m", type=float, default=None, help="eye height above the ground, m (default 10)"
    )
    ap.add_argument(
        "--pan",
        type=float,
        default=None,
        help="how much the camera turns with the storm, 0..1 (default 0.3)",
    )
    ap.add_argument(
        "--pitch", type=float, default=None, help="look up by this many degrees (default 12)"
    )
    ap.add_argument(
        "--sun-azimuth",
        type=float,
        default=None,
        help="light direction chosen for the shot (degrees); default: the real sun or the dusk light",
    )
    ap.add_argument(
        "--sun-elevation", type=float, default=None, help="elevation of that light (degrees)"
    )
    ap.add_argument(
        "--timelapse",
        type=float,
        default=None,
        help="time-lapse factor (clip length = data span / factor + 3 s), overrides --duration",
    )
    ap.add_argument(
        "--look",
        default=None,
        help="a look file (.json or .toml): any keys of docs/REALISTIC_3D.md, the others keep their defaults",
    )
    ap.add_argument(
        "--look-preset",
        default=None,
        help="a built-in look preset by name, e.g. 'Hurricane (stormy)' (a file and --set go on top)",
    )
    ap.add_argument(
        "--set",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help="one look setting, highest priority (repeat), e.g. --set exposure=1.6 --set temperature=0.2",
    )
    ap.add_argument(
        "--sea",
        action="store_true",
        help="ocean with whitecaps and spray from the retrieved surface wind",
    )
    ap.add_argument(
        "--cloud-freq",
        default=None,
        help="detail noise frequency per km, horizontal,vertical (a cyclone is layered: 0.9,1.8)",
    )
    ap.add_argument(
        "--no-flow", action="store_true", help="translation only, even if the bundle has winds"
    )
    ap.add_argument("--no-lightning", action="store_true", help="no lightning")
    ap.add_argument(
        "--lightning-seed", type=int, default=7, help="seed of the modelled flashes (deterministic)"
    )
    ap.add_argument(
        "--lightning-rate", type=float, default=0.8, help="most modelled flashes per clip second"
    )
    ap.add_argument(
        "--t10-km",
        type=float,
        default=5.0,
        help="height of the -10 C level (km ASL) for the flash model",
    )
    ap.add_argument(
        "--no-sound",
        action="store_true",
        help="no audio track (the observer clip has one by default)",
    )
    ap.add_argument(
        "--sound-compress",
        type=float,
        default=8.0,
        help="thunder delay = distance / 343 m/s / this factor (1 = real physics, longer than a time-lapse clip)",
    )
    ap.add_argument("--title", default=None, help="title at the top left (default: the bundle's)")
    ap.add_argument("--subtitle", default=None, help="line under the title")
    ap.add_argument(
        "--map-theme", default="dark", choices=["dark", "light"], help="ground map restyle"
    )
    ap.add_argument(
        "--basemap", default="osm", help="basemap provider id (see src/lib/utils/basemaps.ts)"
    )
    ap.add_argument(
        "--ground",
        default="map",
        choices=["map", "realistic"],
        help="flat map (default) or terrain with Sentinel-2 satellite imagery",
    )
    ap.add_argument(
        "--storm-style",
        default="scientific",
        choices=["scientific", "realistic"],
        help="colour scale (default) or the stylised precipitation look with sun light",
    )
    ap.add_argument(
        "--realistic", action="store_true", help="--ground realistic --storm-style realistic"
    )
    ap.add_argument(
        "--tile-cache",
        default=None,
        help="folder of cached tiles (default ~/.cache/xradar-desktop/tiles)",
    )
    ap.add_argument("--offline", action="store_true", help="use cached tiles only, no network")
    ap.add_argument("--crf", type=int, default=18, help="x264 quality (lower = better)")
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--stills", default=None, help="folder for PNG stills")
    ap.add_argument("--stills-at", default="0,9,18,26,31,34.5", help="clip times (s) of the stills")
    ap.add_argument("--max-frames", type=int, default=0, help="stop after this many frames (tests)")
    ap.add_argument(
        "--every",
        type=int,
        default=1,
        help="render only every Nth frame (a quick preview, not a real-time clip)",
    )
    asyncio.run(render(ap.parse_args(argv)))


if __name__ == "__main__":
    main()

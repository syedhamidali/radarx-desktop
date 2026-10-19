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


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the built app at / and the data bundle at /bundle/."""

    def __init__(self, *args, dist: Path, bundle: Path, **kw):
        self._dist, self._bundle = dist, bundle
        super().__init__(*args, **kw)

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/bundle/"):
            return str(self._bundle / path[len("/bundle/") :].lstrip("/"))
        return str(self._dist / path.lstrip("/"))

    def log_message(self, *args) -> None:  # quiet
        pass


def serve(dist: Path, bundle: Path) -> tuple[socketserver.TCPServer, int]:
    handler = functools.partial(_Handler, dist=dist, bundle=bundle)
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
    srv, port = serve(dist, bundle)
    W, H, fps = a.width, a.height, a.fps
    settings = {"fps": fps, "duration": a.duration, "vexag": a.vexag}
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
        info = await page.evaluate("(a) => window.__xrClip.load(a[0], a[1])", ["/bundle", settings])
        print(f"loaded {info['steps']} time steps, grid {info['grid']}, '{info['title']}'")
        await page.evaluate("(p) => window.__xrClip.prepare(p)", a.basemap)
        st = await page.evaluate("() => window.__xrClip.info()")
        if st["fatal"]:
            sys.exit(f"renderer error: {st['fatal']}")
        print(
            f"renderer ready at {st['w']}x{st['h']}, map {'ok' if st['mapOk'] else 'MISSING (grid ground)'}"
        )
        n = await page.evaluate("() => window.__xrClip.frameCount()")
        if a.max_frames:
            n = min(n, a.max_frames)

        container = av.open(str(out), "w", options={"movflags": "+faststart"})
        stream = container.add_stream("libx264", rate=fps)
        stream.width, stream.height = W - W % 2, H - H % 2
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": str(a.crf), "preset": a.preset}
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
            if i % 30 == 0 or i == n - 1:
                el = time.time() - t0
                print(
                    f"frame {i + 1}/{n}  {el:.0f}s elapsed, {el / (i // a.every + 1):.2f}s per frame, ETA {(n - i - 1) / a.every * el / (i // a.every + 1):.0f}s",
                    flush=True,
                )
        for pkt in stream.encode():
            container.mux(pkt)
        container.close()
        await browser.close()
    srv.shutdown()
    if logs:
        print("browser messages:\n  " + "\n  ".join(logs[-10:]))
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
    ap.add_argument("--vexag", type=float, default=4.0, help="vertical exaggeration of the scene")
    ap.add_argument("--title", default=None, help="title at the top left (default: the bundle's)")
    ap.add_argument("--subtitle", default=None, help="line under the title")
    ap.add_argument(
        "--map-theme", default="dark", choices=["dark", "light"], help="ground map restyle"
    )
    ap.add_argument(
        "--basemap", default="osm", help="basemap provider id (see src/lib/utils/basemaps.ts)"
    )
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

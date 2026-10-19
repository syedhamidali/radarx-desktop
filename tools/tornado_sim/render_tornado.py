"""Render a test clip (H.264 MP4) of the tornado simulation in headless Chrome.

Same approach as tools/storm_clip/render_clip.py: the page (``/?tornado=1``) draws frame i for the shot time
i / fps in one pass and the script grabs the pixels, so the result does not depend on how fast the machine is.
Needs a scratch Python environment OUTSIDE the repository with ``playwright``, ``av`` and ``pillow``::

    npm run build
    /path/to/venv_clip/bin/python tools/tornado_sim/render_tornado.py --mode chaser --out chaser.mp4

``--seed`` is the JSON written by tools/tornado_sim/make_seed.py. Tiles (satellite imagery, terrain) come from
the tile cache (``--tile-cache``, offline by default); without them the ground is procedural and says so.
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
sys.path.insert(0, str(REPO / "python"))
import itertools

from engine.animate_tiles import TileCache


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, dist: Path, tiles: TileCache, **kw):
        self._dist, self._tiles = dist, tiles
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
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        return str(self._dist / path.lstrip("/"))

    def log_message(self, *args) -> None:
        pass


def serve(dist: Path, tiles: TileCache):
    handler = functools.partial(_Handler, dist=dist, tiles=tiles)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


async def main_async(a: argparse.Namespace) -> None:
    import av
    from PIL import Image
    from playwright.async_api import async_playwright

    dist = Path(a.app_dist).expanduser().resolve()
    if not (dist / "index.html").exists():
        sys.exit(f"{dist}/index.html not found: run `npm run build` first")
    seed_doc = json.loads(Path(a.seed).expanduser().read_text())
    seed = seed_doc["seed"]
    out = Path(a.out).expanduser().resolve() if a.out else None
    stills = Path(a.stills).expanduser().resolve() if a.stills else None
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
    if stills:
        stills.mkdir(parents=True, exist_ok=True)
    tiles = TileCache(a.tile_cache, offline=not a.online)
    srv, port = serve(dist, tiles)
    W, H, fps = a.width, a.height, a.fps
    n = round(a.duration * fps)
    import numpy as np

    cp = tl_clip = tl_data = cp_t = None
    if a.mode == "movie":
        movie = Path(a.movie).expanduser()
        raw = json.loads((movie / "camera_path.json").read_text())
        cp_t = [c["t_clip_s"] for c in raw]
        keys = ("lat", "lon", "alt_m", "heading_deg", "pitch_deg") + (
            ("fov_deg",) if "fov_deg" in raw[0] else ()
        )
        cp = {k: [c[k] for c in raw] for k in keys}
        # headings wrap at 360: unwrap so the interpolation does not spin the camera
        cp["heading_deg"] = list(np.degrees(np.unwrap(np.radians(cp["heading_deg"]))))
        tl = json.loads((movie / "timeline.json").read_text())["mapping"]
        tl_clip, tl_data = tl["t_clip_s"], tl["t_data_s"]
        a.t0 = float(np.interp(a.clip_start, tl_clip, tl_data))
        a.speed = (
            float(np.interp(a.clip_start + a.duration, tl_clip, tl_data)) - a.t0
        ) / a.duration  # mean data s per clip s
    still_set = (
        {round(float(x) * fps): float(x) for x in a.stills_at.split(",")} if a.stills_at else {}
    )
    t0, speed = a.t0, a.speed
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
        page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        logs: list[str] = []
        page.on(
            "console",
            lambda m: (
                logs.append(f"{m.type}: {m.text[:400]}") if m.type in ("error", "warning") else None
            ),
        )
        page.on("pageerror", lambda e: logs.append(f"PAGEERROR: {str(e)[:400]}"))
        await page.goto(f"http://127.0.0.1:{port}/?tornado=1")
        await page.wait_for_function("() => !!window.__tornado", timeout=30000)
        cfg = {
            "seed": seed,
            "width": W,
            "height": H,
            "ambient": a.ambient,
            "tiles": not a.no_tiles,
            "tileBase": "",
            "shot": [t0, t0 + a.duration * speed],
            "renderScale": a.render_scale,
            "debrisCount": a.debris,
        }
        try:
            init = await page.evaluate("(c) => window.__tornado.init(c)", cfg)
        except Exception as exc:
            print("browser messages:\n  " + "\n  ".join(logs[-10:]))
            sys.exit(f"init failed: {str(exc)[:600]}")
        info = init["info"]
        print("ground:", init["ground"]["log"], "mode:", info["ground"])
        print(
            f"EF{info['ef'][-1]} vt {info['vt']:.1f} m/s, core radius {info['coreRadius']:.0f} m ({info['coreSource']}), "
            f"core spin {info['periodCore']:.1f} s, funnel radius at 60 m {info['funnelRadius60']:.0f} m, "
            f"LCL {info['lcl']:.0f} m, cloud base {info['cloudBase']:.0f} m, translation {info['speed']:.1f} m/s toward {info['headingDeg']:.0f} deg"
        )
        if a.mode != "movie":
            await page.evaluate(
                "(a) => window.__tornado.prepare(a)",
                {"mode": a.mode, "t0": t0, "speed": speed, "duration": a.duration},
            )
        if a.mask != 63:
            await page.evaluate("(m) => window.__tornado.mask(m)", a.mask)
        await page.evaluate(
            "(o) => window.__tornado.tune(o)", {"exposure": a.exposure, "flash": a.flash}
        )
        container = None
        stream = None
        if out:
            container = av.open(str(out), "w", options={"movflags": "+faststart"})
            stream = container.add_stream("libx264", rate=fps)
            stream.width, stream.height = W - W % 2, H - H % 2
            stream.pix_fmt = "yuv420p"
            stream.options = {"crf": str(a.crf), "preset": "medium"}
        t_start = time.time()
        ms = []
        last = None
        report: list[dict] = []
        frames = sorted(still_set) if a.only_stills else range(0, n, a.every)
        for i in frames:
            tau = i / fps
            t = t0 + tau * speed
            if a.mode == "movie":
                tc = a.clip_start + tau
                g = {k: float(np.interp(tc, cp_t, cp[k])) for k in cp}
                geo = {
                    "lat": g["lat"],
                    "lon": g["lon"],
                    "altM": g["alt_m"],
                    "headingDeg": g["heading_deg"],
                    "pitchDeg": g["pitch_deg"],
                    "fovDeg": g.get("fov_deg", a.fov),
                }
                t = float(np.interp(tc, tl_clip, tl_data))
                res = await page.evaluate(
                    "(a) => window.__tornado.frameGeo(a)",
                    {
                        "geo": geo,
                        "t": t,
                        "labels": not a.no_labels and not a.overlay,
                        "tau": tau,
                        "duration": a.duration,
                        "overlay": a.overlay,
                    },
                )
            else:
                res = await page.evaluate(
                    "(a) => window.__tornado.frame(a)",
                    {
                        "mode": a.mode,
                        "tau": tau,
                        "t": t,
                        "duration": a.duration,
                        "labels": not a.no_labels,
                    },
                )
            last = res
            ms.append(res["stats"]["ms"])
            if a.overlay and stills:
                import base64

                data = await page.evaluate("() => window.__tornado.png()")
                (stills / f"overlay_{tau:05.1f}s.png").write_bytes(
                    base64.b64decode(data.split(",", 1)[1])
                )
            png = await page.screenshot(type="png")
            img = Image.open(io.BytesIO(png)).convert("RGB")
            luma = float(sum(img.convert("L").resize((48, 27)).getdata())) / (48 * 27)
            report.append(
                {
                    "frame": i,
                    "tau": tau,
                    "t_data": t,
                    "luma": luma,
                    "ms": res["stats"]["ms"],
                    "wind_cam": res["pose"]["wind"],
                    "shake_deg": res["pose"]["shakeDeg"],
                    "distance": res["pose"]["distance"],
                    "airborne": res["stats"]["debrisAirborne"],
                }
            )
            if i in still_set and stills:
                img.save(stills / f"{a.mode}_{still_set[i]:05.1f}s.png")
            if stream is not None:
                if img.size != (stream.width, stream.height):
                    img = img.crop((0, 0, stream.width, stream.height))
                for pkt in stream.encode(av.VideoFrame.from_image(img)):
                    container.mux(pkt)
            if a.only_stills:
                po = res["pose"]
                print(
                    f"still tau={tau:.2f} t={t:.2f}: camera {[round(x, 1) for x in po['pos']]} target {[round(x, 1) for x in po['target']]} fov {po['fov']} dist {po['distance']:.0f} m wind {po['wind']:.1f} m/s shake {po['shakeDeg']:.2f} deg",
                    flush=True,
                )
            if i % 30 == 0:
                print(
                    f"frame {i}/{n} t={t:.1f} s  gpu {res['stats']['ms']:.0f} ms  airborne {res['stats']['debrisAirborne']}  wind@cam {res['pose']['wind']:.1f} m/s  dist {res['pose']['distance']:.0f} m",
                    flush=True,
                )
        if stream is not None:
            for pkt in stream.encode():
                container.mux(pkt)
            container.close()
        st = last["stats"] if last else {}
        if report:
            lum = [r["luma"] for r in report]
            d = [abs(b - a) for a, b in itertools.pairwise(lum)]
            summ = {
                "frames": len(report),
                "luma_min": min(lum),
                "luma_max": max(lum),
                "max_frame_to_frame_luma_change": max(d) if d else 0.0,
                "mean_ms": sum(ms) / len(ms),
                "max_ms": max(ms),
                "gpu_mb": st.get("gpuMB"),
                "js_heap_mb": st.get("jsHeapMB"),
            }
            print("report:", json.dumps(summ))
            if a.report:
                Path(a.report).expanduser().write_text(
                    json.dumps({"summary": summ, "frames": report}, indent=1)
                )
        el = time.time() - t_start
        print(
            f"mean render {sum(ms) / max(1, len(ms)):.0f} ms/frame (GPU, incl. finish), {el / max(1, len(ms)):.2f} s per frame with capture; gpu {st.get('gpuMB', 0):.0f} MB, js heap {st.get('jsHeapMB')}"
        )
        await browser.close()
    srv.shutdown()
    if logs:
        print("browser messages:\n  " + "\n  ".join(logs[-10:]))
    if out:
        print(
            f"wrote {out}  ({n // a.every} frames, {n / fps:.1f} s at {fps} fps, {W}x{H}, {out.stat().st_size / 1e6:.1f} MB)"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--seed",
        default=str(
            Path.home() / "Downloads/xradar-exports/perils_movie/tornado_sim/tornado_seed.json"
        ),
    )
    ap.add_argument(
        "--mode",
        choices=["chaser", "debris", "inside", "movie"],
        default="chaser",
        help="'movie' follows the camera path and time map of the PERiLS movie (--movie, --clip-start)",
    )
    ap.add_argument("--movie", default=str(Path.home() / "Downloads/xradar-exports/perils_movie"))
    ap.add_argument(
        "--clip-start",
        type=float,
        default=46.0,
        help="movie clip time (s) at the start of the shot (mode movie)",
    )
    ap.add_argument(
        "--fov", type=float, default=50.0, help="vertical field of view (deg) for the movie camera"
    )
    ap.add_argument("--out", default=None)
    ap.add_argument("--stills", default=None)
    ap.add_argument("--stills-at", default=None, help="comma separated shot times (s)")
    ap.add_argument("--app-dist", default=str(REPO / "dist"))
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--duration", type=float, default=12.0)
    ap.add_argument(
        "--t0", type=float, default=440.0, help="data time (s) at the start of the shot"
    )
    ap.add_argument(
        "--speed", type=float, default=1.0, help="data seconds per shot second (1 = real time)"
    )
    ap.add_argument("--ambient", default="dusk", choices=["dusk", "night", "overcast"])
    ap.add_argument("--every", type=int, default=1)
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--render-scale", type=float, default=1.0)
    ap.add_argument("--debris", type=int, default=6000)
    ap.add_argument("--tile-cache", default=None)
    ap.add_argument(
        "--online",
        action="store_true",
        help="allow the tile proxy to download missing tiles (default: cache only)",
    )
    ap.add_argument("--no-tiles", action="store_true")
    ap.add_argument("--no-labels", action="store_true")
    ap.add_argument(
        "--overlay",
        action="store_true",
        help="mode movie: also save the tornado alone with alpha (overlay_*.png in --stills) for compositing over another render",
    )
    ap.add_argument(
        "--report", default=None, help="write per-frame stats (luma, ms, wind, shake) as JSON"
    )
    ap.add_argument(
        "--exposure", type=float, default=1.0, help="multiplier on the exposure of the look preset"
    )
    ap.add_argument(
        "--flash",
        type=float,
        default=1.0,
        help="multiplier on the lightning illumination (0 = no lightning light)",
    )
    ap.add_argument(
        "--mask",
        type=int,
        default=63,
        help="diagnostics: bit mask of volume components (1 haze, 2 cloud, 4 funnel, 8 rain, 16 dust, 32 debris)",
    )
    ap.add_argument(
        "--only-stills",
        action="store_true",
        help="render only the frames of --stills-at (the debris warm-up still runs from the shot start)",
    )
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()

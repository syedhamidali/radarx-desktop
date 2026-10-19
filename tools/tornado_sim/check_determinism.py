"""Determinism check of the tornado renderer: frame i of the clip time i / fps is always the same picture.

Three checks, each on a hash of the canvas pixels (the page's ``hashPixels``):
  1. the same frame twice in a row,
  2. a frame, other frames (the simulation clock goes backwards and forwards), the first frame again,
  3. a frame reached by stepping 1/30 s at a time from the start of the shot, and the same frame in a fresh page
     that jumps straight to it (the debris pool is re-run from the shot start, so both must agree).

    npm run build
    /path/to/venv_clip/bin/python tools/tornado_sim/check_determinism.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine.animate_tiles import TileCache
from render_tornado import serve


async def open_page(p, port, seed, size):
    browser = await p.chromium.launch(
        channel="chrome",
        headless=True,
        args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"],
    )
    page = await browser.new_page(
        viewport={"width": size[0], "height": size[1]}, device_scale_factor=1
    )
    await page.goto(f"http://127.0.0.1:{port}/?tornado=1")
    await page.wait_for_function("() => !!window.__tornado")
    await page.evaluate(
        "(c) => window.__tornado.init(c)",
        {
            "seed": seed,
            "width": size[0],
            "height": size[1],
            "tiles": False,
            "shot": [450, 462],
            "debrisCount": 4000,
        },
    )
    return browser, page


async def frame(page, mode, tau, t0=450.0):
    await page.evaluate(
        "(a) => window.__tornado.frame(a)",
        {"mode": mode, "tau": tau, "t": t0 + tau, "duration": 12, "labels": False},
    )
    return await page.evaluate("() => window.__tornado.hashPixels()")


async def main_async(a: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    seed = json.loads(Path(a.seed).expanduser().read_text())["seed"]
    srv, port = serve(Path(a.app_dist).expanduser().resolve(), TileCache(None, offline=True))
    ok = True
    size = (640, 360)
    async with async_playwright() as p:
        browser, page = await open_page(p, port, seed, size)
        results = {}
        for mode in ("chaser", "debris", "inside"):
            h1 = await frame(page, mode, 5.0)
            h2 = await frame(page, mode, 5.0)
            await frame(page, mode, 2.0)  # the clock goes back
            await frame(page, mode, 8.5)
            h3 = await frame(page, mode, 5.0)
            same = h1 == h2 == h3
            ok &= same
            results[mode] = {"twice": h1 == h2, "after_other_frames": h1 == h3, "hash": h1}
            print(
                f"{mode:7s}: same frame twice {h1 == h2}, after other frames {h1 == h3}  (hash {h1})"
            )
        # sequential vs jump
        n = 45
        last = None
        for i in range(n + 1):
            last = await frame(page, "debris", i / 30.0 + 4.0)
        await browser.close()
        browser2, page2 = await open_page(p, port, seed, size)
        jump = await frame(page2, "debris", n / 30.0 + 4.0)
        await browser2.close()
        seq_ok = last == jump
        ok &= seq_ok
        print(
            f"sequential 45 steps vs a fresh page jumping straight to the frame: {seq_ok}  ({last} vs {jump})"
        )
        results["sequential_vs_jump"] = seq_ok
    srv.shutdown()
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=1))
    print("DETERMINISTIC" if ok else "NOT DETERMINISTIC")
    return 0 if ok else 1


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
    ap.add_argument("--app-dist", default=str(REPO / "dist"))
    ap.add_argument("--out", default=None)
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()

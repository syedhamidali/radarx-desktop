#!/usr/bin/env python3
"""Pixel check of the 2D PPI canvas: hashes of the WebGL canvas for a few sweeps, in dark and light.

Run it against two builds (the perf build of each) and compare the lines; identical hashes mean the
renderer output did not change:

    python scripts/perf/ppi_hash.py --url http://127.0.0.1:4174/ --engine-port 25611 --file KTLX20130520_201643_V06.gz

The map is switched off (no network tiles) and the colour bar / toasts are hidden, so only the canvas
is compared. The same machine, browser and GPU are needed for a meaningful comparison.
"""
import argparse
import hashlib
import sys
import tempfile
import os

from playwright.sync_api import sync_playwright

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from measure_runtime import Bench  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4174/")
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--sweeps", default="0,3,6")
    a = ap.parse_args()
    sweeps = [int(s) for s in a.sweeps.split(",")]
    with sync_playwright() as p:
        for theme in ("dark", "light"):
            b = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"])
            ctx = b.new_context(viewport={"width": 1500, "height": 950}, color_scheme=theme)
            page = ctx.new_page()
            page.add_init_script(f"window.__XRADAR_PORT__ = {a.engine_port};")
            page.goto(a.url)
            page.wait_for_function("window.__xr && window.__xr.wsManager.isConnected", timeout=30000)
            bench = Bench(page, ctx.new_cdp_session(page))
            bench.skip_tour()
            page.keyboard.press("Control+Shift+m")  # no map: no network tiles
            bench.open(a.file)
            page.wait_for_timeout(2500)
            page.mouse.move(5, 5)
            page.add_style_tag(content=".colorbar-container, [class*=toast], .hud-readout {visibility:hidden !important}")
            hashes = []
            for sw in sweeps:
                page.evaluate("(s) => window.__xr.selectedSweep.set(s)", sw)
                page.wait_for_timeout(1500)
                path = os.path.join(tempfile.gettempdir(), f"ppi_hash_{theme}_{sw}.png")
                page.locator(".webgl-canvas").first.screenshot(path=path)
                hashes.append(hashlib.md5(open(path, "rb").read()).hexdigest()[:12])
                os.unlink(path)
            print(theme, hashes)
            b.close()


if __name__ == "__main__":
    main()

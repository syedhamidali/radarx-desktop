#!/usr/bin/env python3
"""Which kinds of DOM element accumulate over repeated open / 3D / close cycles? Prints the element
classes whose count grew between cycle 4 and cycle 14 (a growing count that should be flat is a leak).

    python scripts/perf/domdiff.py --url http://127.0.0.1:4174/ --engine-port N --file F --file2 G
"""
import argparse
import collections
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from measure_runtime import INIT, Bench  # noqa: E402

HIST = """() => { const h = {}; for (const e of document.querySelectorAll('*')) {
  const k = e.tagName + '.' + (e.getAttribute('class') || '').split(' ').filter(c => !c.startsWith('svelte-')).join('.');
  h[k] = (h[k] || 0) + 1; } return h; }"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4174/")
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--file2", default="")
    a = ap.parse_args()
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"])
        ctx = b.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.add_init_script(INIT)
        page.add_init_script(f"window.__XRADAR_PORT__ = {a.engine_port};")
        cdp = ctx.new_cdp_session(page)
        page.goto(a.url)
        page.wait_for_function("window.__xr && window.__xr.wsManager.isConnected")
        bench = Bench(page, cdp)
        bench.skip_tour()
        files = [a.file] + ([a.file2] if a.file2 else [])
        snaps = {}
        for i in range(14):
            bench.open(files[i % len(files)])
            if i % 2 == 0:
                bench.view("3d")
                bench.view("2d")
            bench.close_all()
            page.wait_for_timeout(300)
            if i in (3, 13):
                snaps[i] = page.evaluate(HIST)
        x, y = snaps[3], snaps[13]
        d = collections.Counter({k: y.get(k, 0) - x.get(k, 0) for k in set(x) | set(y)})
        print("grown element classes (cycle 14 minus cycle 4):", [(k, v) for k, v in d.most_common(12) if v])
        print("elements in document:", page.evaluate("document.querySelectorAll('*').length"))
        b.close()


if __name__ == "__main__":
    main()

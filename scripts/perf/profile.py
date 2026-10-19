#!/usr/bin/env python3
"""CPU profile of the main thread while panning the 2D map and orbiting the 3D view; prints the
functions with the most self time. Use a non-minified build so the names are readable:

    PERF_NOMINIFY=1 PERF_OUT=dist-prof npx vite build -c scripts/perf/vite.perf.config.ts
    npx vite preview --outDir dist-prof --host 127.0.0.1 --port 4176 &
    python scripts/perf/profile.py --url http://127.0.0.1:4176/ --engine-port N --file F
"""
import argparse
import collections
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from measure_runtime import Bench  # noqa: E402


def top(profile, n=22):
    nodes = {nd["id"]: nd for nd in profile["nodes"]}
    self_us = collections.Counter()
    dt = profile["timeDeltas"]
    for sid, d in zip(profile["samples"], dt):
        self_us[sid] += d
    agg = collections.Counter()
    for sid, us in self_us.items():
        cf = nodes[sid]["callFrame"]
        key = f'{cf["functionName"] or "(anon)"}  {cf["url"].split("/")[-1]}:{cf["lineNumber"]}'
        agg[key] += us
    total = sum(agg.values())
    idle = sum(v for k, v in agg.items() if k.startswith("(idle)"))
    print(f"  total sampled {total/1000:.0f} ms, idle {idle/1000:.0f} ms")
    for k, v in agg.most_common(n):
        print(f"  {v/1000:8.1f} ms  {100*v/total:5.1f}%  {k}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4176/")
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--cpu", type=float, default=1)
    ap.add_argument("--seconds", type=float, default=6)
    a = ap.parse_args()
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"])
        ctx = b.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.add_init_script(f"window.__XRADAR_PORT__ = {a.engine_port};")
        cdp = ctx.new_cdp_session(page)
        page.goto(a.url)
        page.wait_for_function("window.__xr && window.__xr.wsManager.isConnected", timeout=30000)
        bench = Bench(page, cdp)
        bench.skip_tour()
        bench.open(a.file)
        if a.cpu > 1:
            cdp.send("Emulation.setCPUThrottlingRate", {"rate": a.cpu})
        for view, label in (("2d", "pan/zoom 2D"), ("3d", "orbit 3D")):
            bench.view(view)
            page.wait_for_timeout(2500)
            cdp.send("Profiler.enable")
            cdp.send("Profiler.setSamplingInterval", {"interval": 200})
            cdp.send("Profiler.start")
            bench.drag_loop(a.seconds, radius=150, wheel=(view == "2d"))
            prof = cdp.send("Profiler.stop")["profile"]
            print(f"\n== {label}")
            top(prof)
        b.close()


if __name__ == "__main__":
    main()

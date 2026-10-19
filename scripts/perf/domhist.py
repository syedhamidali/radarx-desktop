#!/usr/bin/env python3
"""Element-class histogram difference between two builds right after load:
    python scripts/perf/domhist.py URL_A URL_B --engine-port N"""
import argparse
import collections

from playwright.sync_api import sync_playwright

HIST = """() => { const h = {}; for (const e of document.querySelectorAll('*')) {
  const k = e.tagName + '.' + (e.getAttribute('class') || '').split(' ').filter(c => !c.startsWith('svelte-')).join('.');
  h[k] = (h[k] || 0) + 1; } return h; }"""

ap = argparse.ArgumentParser()
ap.add_argument("a")
ap.add_argument("b")
ap.add_argument("--engine-port", type=int, required=True)
args = ap.parse_args()
res = []
with sync_playwright() as p:
    br = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server"])
    for u in (args.a, args.b):
        ctx = br.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.add_init_script(f"window.__XRADAR_PORT__ = {args.engine_port};")
        page.goto(u)
        page.wait_for_timeout(5000)
        try:
            page.get_by_text("Skip", exact=True).first.click(timeout=1500)
        except Exception:
            pass
        page.wait_for_timeout(500)
        res.append(page.evaluate(HIST))
        ctx.close()
    br.close()
x, y = res
d = collections.Counter({k: y.get(k, 0) - x.get(k, 0) for k in set(x) | set(y)})
print([(k, v) for k, v in sorted(d.items(), key=lambda kv: -abs(kv[1])) if v][:15])

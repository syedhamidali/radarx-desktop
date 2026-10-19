#!/usr/bin/env python3
"""DOM node / JS listener counters right after load (no file) for one or more builds, for a quick
comparison:  python scripts/perf/counters.py http://127.0.0.1:4177/ http://127.0.0.1:4174/ --engine-port N"""
import argparse

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("urls", nargs="+")
ap.add_argument("--engine-port", type=int, required=True)
a = ap.parse_args()
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server"])
    for u in a.urls:
        ctx = b.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.add_init_script(f"window.__XRADAR_PORT__ = {a.engine_port};")
        cdp = ctx.new_cdp_session(page)
        page.goto(u)
        page.wait_for_timeout(5000)
        try:
            page.get_by_text("Skip", exact=True).first.click(timeout=1500)
        except Exception:
            pass
        page.wait_for_timeout(500)
        c = cdp.send("Memory.getDOMCounters")
        print(u, {k: c[k] for k in ("nodes", "jsEventListeners")}, "elements:", page.evaluate("document.querySelectorAll('*').length"))
        ctx.close()
    b.close()

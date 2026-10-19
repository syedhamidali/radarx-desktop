#!/usr/bin/env python3
"""UI smoke test for a built frontend (run before/after bundle changes; used to prove that code
splitting and removals did not break a screen).

    python scripts/perf/smoke.py --url http://127.0.0.1:4174/ --engine-port 25611 --file <radar file>

Checks, with a real engine: boot, welcome screen, open file, 2D viewer canvas, 3D view canvas + no
"3D unavailable", dual-pol dashboard, diff-free view switching back, every right-hand panel
renders its body, command palette / shortcuts / settings / about dialogs open and close, and no
page error or console error occurs. Exits non-zero on the first failure.
"""
import argparse
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from measure_runtime import Bench  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4174/")
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--file", required=True)
    a = ap.parse_args()
    fails, errors = [], []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"])
        ctx = b.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.add_init_script(f"window.__XRADAR_PORT__ = {a.engine_port};")
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)[:200]))
        page.on("console", lambda m: errors.append("console: " + m.text[:200]) if m.type == "error" and "favicon" not in m.text else None)
        cdp = ctx.new_cdp_session(page)
        page.goto(a.url)
        page.wait_for_function("window.__xr && window.__xr.wsManager.isConnected", timeout=30000)
        bench = Bench(page, cdp)
        check("first-run tour appears", page.locator("[aria-label='Welcome tour']").count() > 0 or page.get_by_text("Skip", exact=True).count() > 0)
        bench.skip_tour()
        page.wait_for_timeout(500)
        check("tour closes", page.locator("[aria-label='Welcome tour']").count() == 0)
        check("welcome screen", "Open" in page.inner_text("body"))
        page.wait_for_timeout(3000)  # idle prefetch of the panels
        panels = page.locator(".managed-panel")
        check("14 right-hand panels", panels.count() == 14)
        bodies = page.locator(".managed-panel-content")
        mounted = sum(1 for i in range(bodies.count()) if bodies.nth(i).inner_html().strip() != "")
        check(f"all panel bodies mounted ({mounted}/14)", mounted == 14)

        bench.open(a.file)
        page.wait_for_timeout(1500)
        check("2D canvas", page.locator(".viewer-area canvas").count() >= 1)
        bench.open(a.file)  # a second file: the timeline (its own chunk) appears from two files on
        check("timeline", page.locator(".timeline").count() >= 1)
        bench.view("3d")
        page.wait_for_timeout(2500)
        check("3D viewer", page.locator(".viewer-3d canvas").count() >= 1)
        check("3D has no fatal message", "3D unavailable" not in page.inner_text("body"))
        bench.view("dualpol")
        check("dual-pol dashboard", page.locator(".viewer-area").inner_text().strip() != "")
        bench.view("2d")
        check("back to 2D canvas", page.locator(".viewer-area canvas").count() >= 1)

        for title, what in (("Open data table viewer", "data table"), ("Open metadata tree viewer", "metadata viewer")):
            page.locator(f"button[title='{title}']").first.click()
            try:
                page.wait_for_selector("[role=dialog], .modal, .dt-overlay", timeout=5000)
                ok = True
            except Exception:
                ok = False
            check(what + " opens", ok)
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

        page.keyboard.press("Control+k")
        page.wait_for_selector(".cmd-palette", timeout=5000)
        check("command palette opens", True)
        page.keyboard.press("Escape")
        page.keyboard.press("Control+k") if page.locator(".cmd-palette").count() else None
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        page.keyboard.press("?")
        try:
            page.wait_for_selector(".shortcuts-dialog", timeout=5000)
            check("shortcuts sheet opens", True)
        except Exception:
            check("shortcuts sheet opens", False)
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        page.keyboard.press("Control+,")
        try:
            page.wait_for_selector(".settings-dialog", timeout=5000)
            check("settings opens", True)
        except Exception:
            check("settings opens", False)
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        page.keyboard.press("Control+Shift+a")
        try:
            page.wait_for_selector(".about-dialog", timeout=5000)
            check("about opens", True)
        except Exception:
            check("about opens", False)
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        check("no page / console errors: " + "; ".join(errors[:3]), not errors)
        b.close()
    print("FAILED" if fails else "ALL OK")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

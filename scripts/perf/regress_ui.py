#!/usr/bin/env python3
"""Browser regression checks for the frontend fixes that a node unit test cannot see.

Needs the perf build (it exposes window.__xr), an engine and a radar file:

    npx vite build -c scripts/perf/vite.perf.config.ts && npx vite preview --outDir dist-perf --host 127.0.0.1 --port 4174 &
    python scripts/perf/regress_ui.py --url http://127.0.0.1:4174/ --engine-port 25611 \\
        --file /path/KTLX20130520_201643_V06.gz [--engine-cmd "python python/server.py --port 25699"]

Checks (non-zero exit when one fails):
  * switching to the dual-pol view does not log "Unhandled message type" for replies that have handlers
  * the sidebar splitters are focusable separators: arrows resize, Shift is faster, Home resets, and the
    arrow keys do not also step through sweeps
  * Alt+ArrowUp / Alt+ArrowDown move a right-hand panel and keep focus on its header
  * a typed colour limit of -Infinity / 1e999 is ignored, a valid one is applied
  * every label in the Settings dialog is tied to a control
  * with --engine-cmd: after the engine is killed and restarted, no failed "Loading ... sweep" rows stay in
    the operations tray
"""
import argparse
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from measure_runtime import Bench  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4174/")
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--engine-cmd", default="", help="command that starts an engine; its port must be --engine-port (restart test)")
    a = ap.parse_args()
    fails = []

    def check(name, cond, extra=""):
        print(("ok   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
        if not cond:
            fails.append(name)

    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"])
        ctx = b.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.add_init_script(f"window.__XRADAR_PORT__ = {a.engine_port};")
        logs = []
        page.on("console", lambda m: logs.append(m.text))
        cdp = ctx.new_cdp_session(page)
        page.goto(a.url)
        page.wait_for_function("window.__xr && window.__xr.wsManager.isConnected", timeout=30000)
        bench = Bench(page, cdp)
        bench.skip_tour()
        bench.open(a.file)
        page.wait_for_timeout(1000)

        # 1. replies with handlers are not "unhandled"
        logs.clear()
        bench.view("dualpol")
        page.wait_for_timeout(2500)
        bench.view("2d")
        noisy = [m for m in logs if "Unhandled message type" in m]
        check("dual-pol replies are not logged as unhandled", not noisy, "; ".join(noisy[:2]))

        # 2. sidebar splitters by keyboard
        handle = page.locator(".resize-handle-left")
        check("left splitter is a focusable separator", handle.get_attribute("role") == "separator" and handle.get_attribute("tabindex") == "0")
        sweep0 = page.evaluate("window.__xr.get(window.__xr.selectedSweep)")
        handle.focus()
        v0 = int(handle.get_attribute("aria-valuenow"))
        page.keyboard.press("ArrowRight")
        v1 = int(handle.get_attribute("aria-valuenow"))
        page.keyboard.press("Shift+ArrowRight")
        v2 = int(handle.get_attribute("aria-valuenow"))
        check("arrow widens the left sidebar by 16, Shift by 64", (v1 - v0, v2 - v1) == (16, 64), f"{v0} {v1} {v2}")
        check("arrows on the splitter do not step sweeps", page.evaluate("window.__xr.get(window.__xr.selectedSweep)") == sweep0)
        page.keyboard.press("Home")
        check("Home resets the width", int(handle.get_attribute("aria-valuenow")) == 300)

        # 3. panel reorder keeps focus
        order = lambda: page.evaluate("[...document.querySelectorAll('.managed-panel')].map(e => e.dataset.panelId)")  # noqa: E731
        o0 = order()
        page.locator(".managed-panel-toggle").nth(1).focus()
        page.keyboard.press("Alt+ArrowUp")
        o1 = order()
        check("Alt+ArrowUp moves the panel up", o1[0] == o0[1] and o1[1] == o0[0], f"{o0[:3]} -> {o1[:3]}")
        check("focus stays on the moved panel's header", page.evaluate("document.activeElement?.classList.contains('managed-panel-toggle')"))
        page.keyboard.press("Alt+ArrowDown")
        check("Alt+ArrowDown moves it back", order() == o0)

        # 4. colour bar limits
        page.locator(".range-btn").first.click()
        page.locator(".range-input").first.fill("-Infinity")
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
        shown = page.locator(".range-btn").first.inner_text().strip()
        check("-Infinity is not accepted as the colour minimum", shown == "-20", shown)
        page.locator(".range-btn").first.click()
        page.locator(".range-input").first.fill("-10")
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
        shown = page.locator(".range-btn").first.inner_text().strip()
        check("a valid minimum is applied", shown == "-10", shown)
        ticks = page.locator(".colorbar-strip .tick").all_inner_texts()
        check("colour bar labels keep their decimals", ticks == ["-10", "11.25", "32.5", "53.75", "75"], str(ticks))

        # 5. settings labels
        page.keyboard.press("Control+,")
        page.wait_for_selector(".settings-dialog")
        for tab in ("General", "Display", "Data", "Export"):
            page.locator(".settings-dialog .tab-btn", has_text=tab).click()
            page.wait_for_timeout(150)
            orphans = page.evaluate("[...document.querySelectorAll('.settings-dialog label')].filter(l => !l.control).map(l => l.textContent.trim())")
            check(f"settings/{tab}: every label has a control", not orphans, str(orphans))
        page.keyboard.press("Escape")

        # 6. engine restart
        if a.engine_cmd:
            page.evaluate("(s) => window.__xr.selectedSweep.set(s)", 9)
            subprocess.run(["pkill", "-f", "--", f"server.py --port {a.engine_port}"])
            page.wait_for_timeout(2000)
            for s in (3, 4, 5):  # asked for while the engine is down: these fail with "Not connected"
                page.evaluate("(s) => window.__xr.selectedSweep.set(s)", s)
                page.wait_for_timeout(150)
            eng = subprocess.Popen(a.engine_cmd.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 40
                while time.time() < deadline and page.evaluate("window.__xr.get(window.__xr.openFiles).length") > 0:
                    page.wait_for_timeout(500)
                page.wait_for_timeout(1500)
                failed = page.evaluate("(document.querySelector('footer')?.innerText ?? '').split('\\n').filter((l) => /failed/i.test(l))")
                check("no failed sweep rows after the engine restarted", not failed, str(failed))
            finally:
                eng.kill()

        b.close()
    print("FAILED" if fails else "ALL OK")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

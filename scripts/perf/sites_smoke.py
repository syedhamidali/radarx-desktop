#!/usr/bin/env python3
"""UI smoke test of the Radar sites layer against a built frontend (perf build) and a real engine.

    python scripts/perf/sites_smoke.py --url http://127.0.0.1:4181/ --engine-port 25731 \\
        --file <NEXRAD Level II file> [--shots <dir>]

No network is used and nothing is downloaded: the page's WebSocket is intercepted, so the engine
never sees `get_nexrad_latest_info` (the reply is a canned one) nor any open_nexrad_* message. The
script stops at the download dialog and cancels it.

Checks: the layer is off and its code is not fetched until switched on; markers appear; hover label;
click opens the card; every network step first shows the consent dialog (what, host, size);
cancel changes nothing; "Allow once" -> the card shows the age and the marker turns "recent";
the download dialog states the file size; nothing is sent to the engine to download; label
declutter when zoomed out; keyboard (arrows, Escape); the switch persists across a reload; the 3D
view still works with the layer on; no page or console error.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from measure_runtime import Bench  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4181/")
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--shots", default="")
    a = ap.parse_args()
    fails, errors, to_engine, requests, opened = [], [], [], [], []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    now = datetime.now(timezone.utc)

    def canned(station):
        return json.dumps(
            {
                "type": "nexrad_latest_info",
                "status": "ok",
                "station": station,
                "name": f"{station}{now:%Y%m%d}_{now:%H%M%S}_V06",
                "path": f"s3://unidata-nexrad-level2/{now:%Y/%m/%d}/{station}/{station}{now:%Y%m%d}_{now:%H%M%S}_V06",
                "size": int(14.2 * 1024 * 1024),
                "scan_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "age_s": 120.0,
                "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "s3://unidata-nexrad-level2/",
            }
        )

    def on_ws(ws):
        server = ws.connect_to_server()

        def from_page(msg):
            if isinstance(msg, str):
                try:
                    t = json.loads(msg).get("type")
                except Exception:
                    t = None
                if t:
                    to_engine.append(t)
                if t == "get_nexrad_latest_info":
                    st = json.loads(msg)["station"]
                    ws.send(canned(st))
                    return
                if t == "list_nexrad_l2_files":
                    m = json.loads(msg)
                    ws.send(json.dumps({"type": "nexrad_l2_files", "station": m["station"], "date": m["date"], "files": [
                        {"name": f"{m['station']}20260101_000000_V06", "path": f"s3://unidata-nexrad-level2/2026/01/01/{m['station']}/{m['station']}20260101_000000_V06", "size": 1234567}]}))
                    return
                if t in ("open_nexrad_l2", "open_nexrad_realtime"):
                    opened.append(json.loads(msg))
                    ws.send(json.dumps({"type": "error", "message": f"{t} failed: mocked by the smoke test (nothing was downloaded)"}))
                    return
            server.send(msg)

        ws.on_message(from_page)
        server.on_message(lambda m: ws.send(m))

    def shot(page, name):
        if a.shots:
            page.screenshot(path=f"{a.shots}/sites_{name}.png")

    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"])
        ctx = b.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.add_init_script(f"window.__XRADAR_PORT__ = {a.engine_port};")
        page.route_web_socket(re.compile(rf"ws://(localhost|127\.0\.0\.1):{a.engine_port}"), on_ws)
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)[:200]))
        page.on("console", lambda m: errors.append("console: " + m.text[:200]) if m.type == "error" and "favicon" not in m.text and "tile" not in m.text.lower() and "Failed to load resource" not in m.text and "mocked by the smoke test" not in m.text else None)
        page.on("request", lambda r: requests.append(r.url))
        cdp = ctx.new_cdp_session(page)
        page.goto(a.url)
        page.wait_for_function("window.__xr && window.__xr.wsManager.isConnected", timeout=30000)
        bench = Bench(page, cdp)
        bench.skip_tour()
        page.wait_for_timeout(500)
        bench.open(a.file)
        page.wait_for_timeout(1500)

        # ── off by default, code not fetched ─────────────────────────────────────────────────
        check("layer is off by default (no markers)", page.locator(".rs-dot").count() == 0)
        early = [u for u in requests if "RadarSitesLayer" in u or "radarSites." in u or "radarSites-" in u]
        check("layer code is not fetched before it is switched on " + str(early[:2]), not early)

        toggle = page.locator("label.toggle-label", has_text="Sites").locator("input")
        check("Sites toggle exists", toggle.count() == 1)
        toggle.check()
        page.wait_for_selector(".rs-dot", timeout=15000)
        n_zoom = page.locator(".rs-dot").count()
        check(f"markers appear ({n_zoom})", n_zoom >= 3)
        check("the open file's site is flagged", page.locator(".rs-dot.open").count() == 1)
        check("all markers start neutral (never invented)", page.locator(".rs-dot:not([data-state='unknown'])").count() == 0)
        check("every marker has an accessible name", page.locator(".rs-dot[aria-label*=', ']").count() == n_zoom)

        # ── hover label and the card ─────────────────────────────────────────────────────────
        dot = page.locator(".rs-dot.open")
        sid = dot.get_attribute("data-site")
        dot.hover()
        page.wait_for_selector(".rs-tip", timeout=3000)
        check("hover shows id and name", sid in page.locator(".rs-tip").inner_text())
        dot.click()
        page.wait_for_selector("aside.rs-card", timeout=3000)
        card = page.locator("aside.rs-card")
        check("click opens the site card", sid in card.inner_text() and "open file" in card.inner_text().lower())
        check("card cites the catalogue source and date", "NOAA NCEI HOMR" in card.inner_text() and "retrieved 20" in card.inner_text())
        check("card says the age is not checked yet", "Not checked" in card.inner_text())
        check("no engine call so far", "get_nexrad_latest_info" not in to_engine)
        shot(page, "card")

        # ── consent: cancel changes nothing ──────────────────────────────────────────────────
        card.get_by_role("button", name="Check latest scan").click()
        dlg = page.locator("[role=dialog][aria-labelledby=rsc-title]")
        dlg.wait_for(timeout=3000)
        txt = dlg.inner_text()
        check("consent dialog names the host", "unidata-nexrad-level2" in txt)
        check("consent dialog names the folder and says no radar data", f"/{sid}/" in txt and "No radar data" in txt)
        check("consent dialog states a size", "0.1 MB" in txt)
        shot(page, "consent")
        dlg.get_by_role("button", name="Cancel").click()
        page.wait_for_timeout(300)
        check("cancel closes the dialog", dlg.count() == 0)
        check("cancel sends nothing", "get_nexrad_latest_info" not in to_engine)
        check("marker still neutral", page.locator(".rs-dot.open").get_attribute("data-state") == "unknown")

        # Escape also cancels
        card.get_by_role("button", name="Check latest scan").click()
        dlg.wait_for(timeout=3000)
        page.keyboard.press("Escape")  # even before the dialog has taken the focus
        page.wait_for_timeout(300)
        check("Escape cancels the dialog", dlg.count() == 0 and "get_nexrad_latest_info" not in to_engine)

        # ── Open latest: list consent -> canned answer -> download dialog with the real size ──
        card.get_by_role("button", name="Open latest volume").click()
        dlg.wait_for(timeout=3000)
        dlg.get_by_role("button", name="Allow once").click()
        page.wait_for_selector(".rs-dot.open[data-state='recent']", timeout=5000)
        check("after the answer the marker is recent", True)
        print("   card:", card.inner_text().replace("\n", " | ")[:400])
        check("card shows the age, size and UTC scan time", all(s in card.inner_text() for s in ("2 min old", "14.2 MB", "UTC")))
        check("the engine was asked exactly once (%d)" % to_engine.count("get_nexrad_latest_info"), to_engine.count("get_nexrad_latest_info") == 1)
        dlg.wait_for(timeout=3000)
        dtxt = dlg.inner_text()
        check("download dialog states the file and size", "14.2 MB" in dtxt and "_V06" in dtxt)
        shot(page, "download")
        dlg.get_by_role("button", name="Cancel").click()
        page.wait_for_timeout(500)
        check("the page's socket was really intercepted (%d messages seen)" % len(to_engine), len(to_engine) > 5)
        check("nothing was downloaded or opened", not any(t in to_engine for t in ("open_nexrad_l2", "open_nexrad_realtime")))

        # ── session consent: no further dialogs ──────────────────────────────────────────────
        card.get_by_role("button", name="Check again").click()
        dlg.wait_for(timeout=3000)
        dlg.get_by_role("button", name="Allow for this session").click()
        page.wait_for_timeout(500)
        card.get_by_role("button", name="Check again").click()
        page.wait_for_timeout(600)
        check("session consent: no dialog on the next check", dlg.count() == 0)
        check("session consent: the engine was asked again", to_engine.count("get_nexrad_latest_info") >= 3)

        # ── hand-off to the Cloud panel (the engine's reply is mocked: nothing is downloaded) ──
        card.get_by_role("button", name="Browse archive").click()
        page.get_by_text("20260101_000000_V06").first.wait_for(timeout=5000)
        check("Browse archive: Cloud panel lists the station's folder", "20260101_000000_V06" in page.inner_text("body"))
        check("Browse archive: the Archive tab with the station selected", "list_nexrad_l2_files" in to_engine)
        card.get_by_role("button", name="Open latest volume").click()
        page.wait_for_timeout(800)
        check("Open latest: exactly the shown file is requested from the engine", len(opened) == 1 and opened[0]["type"] == "open_nexrad_l2" and opened[0]["path"].startswith(f"s3://unidata-nexrad-level2/") and opened[0]["path"].endswith("_V06") and f"/{sid}/" in opened[0]["path"])

        # ── keyboard ─────────────────────────────────────────────────────────────────────────
        page.locator(".rs-dot.open").focus()
        page.keyboard.press("ArrowRight")
        page.keyboard.press("ArrowLeft")
        moved = page.evaluate("document.activeElement && document.activeElement.classList.contains('rs-dot')")
        check("arrow keys keep focus on a marker", bool(moved))
        check("exactly one marker is a tab stop", page.locator(".rs-dot[tabindex='0']").count() == 1)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        check("Escape closes the card", page.locator("aside.rs-card").count() == 0)

        # ── declutter: zoom out, labels thin out, markers stay ───────────────────────────────
        labels_in = page.locator(".rs-lbl").count()
        cx, cy = bench.center()
        page.mouse.move(cx, cy)
        for _ in range(14):
            page.mouse.wheel(0, 400)
            page.wait_for_timeout(40)
        page.wait_for_timeout(500)
        n_out = page.locator(".rs-dot").count()
        labels_out = page.locator(".rs-lbl").count()
        check(f"zoomed out: more markers ({n_zoom} -> {n_out}), labels thinned ({labels_out}/{n_out})", n_out > n_zoom and labels_out < n_out)
        shot(page, "zoomed_out")
        check("zoomed out: the layer never shows more labels than the cap", labels_out <= 60 + 2)

        # ── Settings: own sites ──────────────────────────────────────────────────────────────
        page.keyboard.press("Control+,")
        page.wait_for_selector(".settings-dialog", timeout=5000)
        page.locator(".tab-btn", has_text="Data").click()
        page.wait_for_selector("input[type=file][aria-label*='radar sites']", timeout=5000)
        csv = "id,name,lat,lon,elev_m,network\nTS1,Test one,40.0,-100.0,185,TEST\nBAD,x,99,1,,\nTS2,Test two,36.5,-92.0,,TEST\n"
        page.locator("input[type=file][aria-label*='radar sites']").set_input_files({"name": "my.csv", "mimeType": "text/csv", "buffer": csv.encode()})
        page.wait_for_selector("text=2 sites loaded from my.csv", timeout=5000)
        check("own CSV: 2 good rows loaded, the bad one reported", "1 rows skipped" in page.inner_text(".settings-dialog") and "latitude out of range" in page.inner_text(".settings-dialog"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        page.wait_for_selector(".rs-dot.custom", timeout=5000)
        check("own sites appear as diamonds", page.locator(".rs-dot.custom").count() >= 1)
        page.locator(".rs-dot.custom").first.click(force=True)
        page.wait_for_selector("aside.rs-card")
        check("own site card: no download, says why", page.get_by_role("button", name="Open latest volume").is_disabled() and "NEXRAD Level II only" in page.locator("aside.rs-card").inner_text())
        page.keyboard.press("Escape")
        page.evaluate("localStorage.removeItem('xradar-radar-sites-custom')")

        # ── persistence ──────────────────────────────────────────────────────────────────────
        page.reload()
        page.wait_for_function("window.__xr && window.__xr.wsManager.isConnected", timeout=30000)
        stored = page.evaluate("localStorage.getItem('xradar-radar-sites-show')")
        check("the switch is persisted", stored == "1")
        page.evaluate("localStorage.removeItem('xradar-radar-sites-show')")

        # ── 3D with the layer on ─────────────────────────────────────────────────────────────
        page.evaluate("localStorage.setItem('xradar-radar-sites-show','1')")
        page.reload()
        page.wait_for_function("window.__xr && window.__xr.wsManager.isConnected", timeout=30000)
        bench = Bench(page, cdp)
        bench.skip_tour()
        bench.open(a.file)
        bench.view("3d")
        page.wait_for_timeout(3000)
        check("3D viewer renders with the layer on", page.locator(".viewer-3d canvas").count() >= 1 and "3D unavailable" not in page.inner_text("body"))
        shot(page, "3d")
        page.evaluate("localStorage.removeItem('xradar-radar-sites-show')")

        check("no page / console errors: " + "; ".join(errors[:3]), not errors)
        b.close()
    print("FAILED" if fails else "ALL OK")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

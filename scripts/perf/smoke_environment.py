#!/usr/bin/env python3
"""UI smoke test of the provenance and environment-data features, with a real engine.

Start the engine with the in-memory fake provider (no network is ever used) and throw-away settings:

    XRADAR_ENV_FAKE=1 XRADAR_ENV_SETTINGS=/tmp/envhome/environment.json \\
    XRADAR_ENV_CACHE_DIR=/tmp/envhome/cache  python python/server.py --port 25612
    npx vite build -c scripts/perf/vite.perf.config.ts
    npx vite preview --outDir dist-perf --port 4174
    python scripts/perf/smoke_environment.py --url http://localhost:4174/ --engine-port 25612 \\
        --file KTLX20130531_231434_V06 --cache /tmp/envhome/cache [--shots /tmp/shots]

Checks: the Layers panel lists the open file with an OBS chip whose popover shows the details, a
derived layer (storm cells) appears as RETR, the Skew-T and hodograph render the synthetic example
sounding (SIM chip, labelled numbers), and the consent dialog shows source, size and destination
BEFORE anything is on disk, downloads only after "Download", serves a repeat from the cache without
asking, and downloads nothing when cancelled. Exits non-zero on the first failure.
"""

import argparse
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from measure_runtime import Bench  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:4174/")
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--cache", required=True, help="the engine's XRADAR_ENV_CACHE_DIR")
    ap.add_argument("--shots", default="")
    a = ap.parse_args()
    fails, errors = [], []

    def check(name, cond):
        print(("ok   " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    def files_in_cache(pattern):
        found = []
        for root, _, names in os.walk(a.cache):
            found += [n for n in names if pattern in n]
        return found

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
        bench.skip_tour()
        page.wait_for_timeout(3500)  # idle prefetch of the panels and of the approval dialog

        def shot(name):
            if a.shots:
                os.makedirs(a.shots, exist_ok=True)
                page.screenshot(path=os.path.join(a.shots, name + ".png"))

        panels = page.locator(".managed-panel")
        check("14 right-hand panels (Layers and Sounding added)", panels.count() == 14)
        check("Layers panel present", page.locator("[data-panel-id=layers]").count() == 1)
        check("Sounding panel present", page.locator("[data-panel-id=sounding]").count() == 1)
        layers = page.locator("[data-panel-id=layers]")
        check("Layers panel says what it is when empty", "No layers yet" in layers.inner_text())

        # -- provenance of the open file ------------------------------------------------------------
        bench.open(a.file)
        page.wait_for_timeout(1500)
        chip = layers.locator(".prov-chip[data-kind=observed]").first
        check("file layer has an OBS chip", chip.count() == 1 and "OBS" in chip.inner_text())
        check("file layer lists the radar and a valid time", "UTC" in layers.inner_text() and "Radar" in layers.inner_text())
        chip.click()
        page.wait_for_selector(".prov-pop", timeout=3000)
        pop = page.locator(".prov-pop").inner_text()
        check("popover defines the class", "Measured by an instrument" in pop)
        check("popover shows source and valid time", "SOURCE" in pop.upper() and "VALID" in pop.upper() and "UTC" in pop)
        check("popover states the uncertainty", "UNCERTAINTY" in pop.upper())
        shot("layers_popover")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        check("Escape closes the popover", page.locator(".prov-pop").count() == 0)

        # -- a derived layer -------------------------------------------------------------------------
        page.evaluate("window.__xr.wsManager.send({type:'identify_cells', variable:'DBZH', sweep:0, params:{threshold_dbz:35, min_size_km2:10}})")
        try:
            page.wait_for_function("document.querySelector('[data-layer=cells]') !== null", timeout=30000)
            ok = True
        except Exception:
            ok = False
        check("storm cells appear as a layer", ok)
        if ok:
            cells = layers.locator("[data-layer=cells] .prov-chip")
            check("cells layer is RETR", "RETR" in cells.inner_text())

        # -- soundings -------------------------------------------------------------------------------
        sounding = page.locator("[data-panel-id=sounding]")
        sounding.scroll_into_view_if_needed()
        sounding.get_by_role("button", name="Example").click()
        try:
            page.wait_for_selector("[data-testid=skewt]", timeout=20000)
            ok = True
        except Exception:
            ok = False
        check("Skew-T renders the synthetic sounding", ok)
        if ok:
            t = page.locator("[data-testid=skewt] path.temp").first.get_attribute("d") or ""
            td = page.locator("[data-testid=skewt] path.dewpoint").first.get_attribute("d") or ""
            check("temperature and dew point traces are drawn", len(t) > 200 and len(td) > 200)
            check("hodograph renders", page.locator("[data-testid=hodograph] path.trace").count() >= 2)
            txt = sounding.inner_text()
            check("numbers carry units and the parcel", "J/kg" in txt and "m AGL" in txt and "surface-based" in txt.lower() or "SB" in txt)
            check("states the synthetic class", "SIM" in sounding.locator(".prov-chip").first.inner_text())
            check("states the drift note", "vertical line" in txt or "drifted" in txt)
            layers_txt = layers.inner_text()
            check("sounding listed in Layers as Environment", "environment" in layers_txt.lower() and "sounding" in layers_txt.lower())
            sounding.locator("[data-testid=sounding-table] abbr").first.hover()
            shot("sounding")
            sounding.get_by_role("button", name="Enlarge").click()
            page.wait_for_selector(".big", timeout=3000)
            check("enlarged view opens", page.locator(".big [data-testid=skewt]").count() == 1)
            shot("sounding_enlarged")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            check("enlarged view closes", page.locator(".big").count() == 0)

        # -- the consent flow (fake provider, no network) ----------------------------------------------
        page.evaluate("() => { window.__p1 = window.__xr.requestEnvData({provider: 'fake', variables: ['t2m']}); return null; }")
        try:
            page.wait_for_selector("[data-testid=env-approval]", timeout=10000)
            ok = True
        except Exception:
            ok = False
        check("approval dialog appears", ok)
        if ok:
            dlg = page.locator("[data-testid=env-approval]")
            text = dlg.inner_text()
            check("dialog shows the source", "Fake model" in text)
            check("dialog shows the size", page.locator("[data-testid=env-size]").inner_text().startswith(("4 files", "3 files")) or " files" in page.locator("[data-testid=env-size]").inner_text())
            check("dialog shows the destination", a.cache in page.locator("[data-testid=env-destination]").inner_text())
            check("dialog shows credit and licence", "Synthetic test data" in text and "CC0" in text)
            check("NOTHING is on disk before approval", not files_in_cache("fake_t2m"))
            page.wait_for_timeout(400)
            shot("approval")
            dlg.get_by_role("button", name="Download").click()
            try:
                page.wait_for_function("window.__xr.get(window.__xr.envFlow).phase === 'done'", timeout=20000)
                done = True
            except Exception:
                done = False
            check("download completes after approval", done)
            check("files are on disk now", len(files_in_cache("fake_t2m")) >= 3)
            page.wait_for_selector("[data-testid=env-approval]", state="detached", timeout=5000)
            check("dialog closes", page.locator("[data-testid=env-approval]").count() == 0)
            res = page.evaluate("window.__p1")
            check("the caller gets the files and the provenance", bool(res) and len(res["paths"]) >= 3 and res["provenance"]["kind"] == "model")
            layers_txt = layers.inner_text()
            check("fetched data is listed with a MODEL chip", layers.locator(".prov-chip[data-kind=model]").count() >= 1)

            # repeat: from the cache, no dialog
            page.evaluate("() => { window.__p2 = window.__xr.requestEnvData({provider: 'fake', variables: ['t2m']}); return null; }")
            page.wait_for_timeout(1500)
            check("a cached request needs no approval", page.locator("[data-testid=env-approval]").count() == 0)
            res2 = page.evaluate("window.__p2")
            check("... and still returns the files", bool(res2) and all(res2["from_cache"]))

            # cancel: nothing is downloaded
            page.evaluate("() => { window.__p3 = window.__xr.requestEnvData({provider: 'fake', variables: ['u10']}); return null; }")
            page.wait_for_selector("[data-testid=env-approval]", timeout=10000)
            page.locator("[data-testid=env-approval]").get_by_role("button", name="Cancel", exact=True).click()
            page.wait_for_timeout(600)
            check("cancel closes the dialog", page.locator("[data-testid=env-approval]").count() == 0)
            check("cancel downloads nothing", not files_in_cache("fake_u10"))
            check("the caller gets null", page.evaluate("window.__p3") is None)

        check("no page / console errors: " + "; ".join(errors[:3]), not errors)
        b.close()
    print("FAILED" if fails else "ALL OK")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

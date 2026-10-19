#!/usr/bin/env python3
"""Startup benchmark for the built frontend (headless Chrome via Playwright).

    npm run build && npx vite preview --port 4173 &
    python scripts/perf/measure_startup.py --url http://localhost:4173/ --runs 9 [--cpu 4] [--engine-port 25611]

Per run (fresh browser context, cold HTTP cache, no engine unless --engine-port is given):
  fcp        first-contentful-paint (ms)
  ui         time until the welcome / boot UI is on screen and the first frame after it was drawn
  tti        time-to-interactive proxy: first moment after FCP with a 1 s window free of long tasks
  tbt        total blocking time: sum of (longtask - 50 ms) up to tti
  js_kb      JS bytes transferred before tti (uncompressed over localhost, so this is raw size)
  js_req     number of JS requests before tti
  heap_mb    JS heap used after load settled
Prints the median and p90 over the runs.
"""
import argparse
import json
import statistics
import sys
import time

from playwright.sync_api import sync_playwright

LONGTASK_JS = """
window.__lt = [];
new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__lt.push([e.startTime, e.duration]); })
  .observe({ type: 'longtask', buffered: true });
"""


def one_run(b, url, cpu, engine_port):
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    page = ctx.new_page()
    page.add_init_script(LONGTASK_JS)
    if engine_port:
        page.add_init_script(f"window.__XRADAR_PORT__ = {engine_port};")
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
    if cpu > 1:
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": cpu})
    js = {"bytes": 0, "n": 0, "t": []}
    sizes = {}

    def on_resp(r):
        if r.url.endswith(".js") or ".js?" in r.url:
            sizes[r.url] = time.time()

    page.on("response", on_resp)
    t0 = time.time()
    page.goto(url, wait_until="commit")
    page.wait_for_function("document.body && document.body.innerText.trim().length > 20", timeout=60000)
    # let the page settle: wait until a 1 s window without long tasks (max 20 s)
    page.wait_for_timeout(1200)
    deadline = time.time() + 20
    while time.time() < deadline:
        last_end = page.evaluate("Math.max(0, ...window.__lt.map(x => x[0] + x[1]))")
        now = page.evaluate("performance.now()")
        if now - last_end > 1000:
            break
        page.wait_for_timeout(200)
    m = page.evaluate(
        """() => {
        const fcp = (performance.getEntriesByName('first-contentful-paint')[0] || {}).startTime || 0;
        const lt = window.__lt;
        const res = performance.getEntriesByType('resource').filter(r => /\\.js(\\?|$)/.test(r.name));
        return {fcp, lt, js: res.map(r => [r.name, r.encodedBodySize || r.decodedBodySize || 0]),
                ui: performance.getEntriesByName('xr-ui-ready')[0]?.startTime ?? null,
                heap: performance.memory ? performance.memory.usedJSHeapSize : 0,
                nav: performance.getEntriesByType('navigation')[0]?.domContentLoadedEventEnd ?? 0};
    }"""
    )
    lt = m["lt"]
    # tti: end of the last long task that started within the first 'quiet' stretch after fcp
    tti = m["fcp"]
    for start, dur in sorted(lt):
        if start + dur <= tti:
            continue
        if start - tti > 1000:
            break
        tti = start + dur
    tbt = sum(max(0, d - 50) for s, d in lt if s <= tti)
    ctx.close()
    return {
        "fcp": m["fcp"],
        "dcl": m["nav"],
        "tti": tti,
        "tbt": tbt,
        "longtasks": len(lt),
        "js_kb": sum(x[1] for x in m["js"]) / 1024,
        "js_req": len(m["js"]),
        "heap_mb": m["heap"] / 1048576,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:4173/")
    ap.add_argument("--runs", type=int, default=9)
    ap.add_argument("--cpu", type=float, default=1, help="CPU throttle rate (4 = 4x slower)")
    ap.add_argument("--engine-port", type=int, default=0)
    ap.add_argument("--label", default="")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    rows = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist", "--enable-precise-memory-info"])
        one_run(b, a.url, a.cpu, a.engine_port)  # warm-up (disk cache of the server, JIT of the browser)
        for _ in range(a.runs):
            rows.append(one_run(b, a.url, a.cpu, a.engine_port))
        b.close()
    out = {"label": a.label, "cpu": a.cpu, "runs": a.runs}
    for k in rows[0]:
        v = sorted(r[k] for r in rows)
        out[k] = {"median": round(statistics.median(v), 1), "p90": round(v[min(len(v) - 1, int(0.9 * len(v)))], 1)}
    print(json.dumps(out))
    if a.json:
        json.dump({"summary": out, "rows": rows}, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    sys.exit(main())

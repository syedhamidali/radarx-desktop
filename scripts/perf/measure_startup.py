#!/usr/bin/env python3
"""Startup benchmark for the built frontend (headless Chrome via Playwright).

    npm run build && npx vite preview --port 4173 &
    python scripts/perf/measure_startup.py --url http://localhost:4173/ --runs 9 [--cpu 4] [--engine-port 25611]

Per run (fresh browser context, cold HTTP cache, no engine unless --engine-port is given):
  fcp              first-contentful-paint (ms)
  ui               app shell (status bar + main area) inserted AND two frames painted (ms): usable UI
  js_kb_critical   JS (gzip, as served) the HTML pulls in before the app starts: what first paint waits for
  tbt_to_ui        blocking time (long tasks beyond 50 ms) that started before `ui`
  js_kb_total      all JS fetched by the time the page has settled (includes idle prefetch of panels)
  tbt_total        blocking time over the whole load incl. the idle prefetch / panel mounting
  settled          last network response or long-task end (ms)
  heap_mb          JS heap after settling
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
window.__ui = null;
new MutationObserver((_, mo) => {
  if (document.querySelector('.statusbar') && document.querySelector('.main-content')) {
    // after the next frame, so "ui" means painted, not just inserted
    requestAnimationFrame(() => requestAnimationFrame(() => { window.__ui = performance.now(); }));
    mo.disconnect();
  }
}).observe(document, { childList: true, subtree: true });
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
    page.goto(url, wait_until="commit")
    page.wait_for_function("window.__ui !== null", timeout=60000)
    # let the page settle: wait until a 1 s window without long tasks (max 20 s)
    page.wait_for_timeout(4000)  # the idle prefetch of the panels starts within ~1.5 s of the first paint
    deadline = time.time() + 20
    while time.time() < deadline:
        last_end = page.evaluate(
            "Math.max(0, ...window.__lt.map(x => x[0] + x[1]), ...performance.getEntriesByType('resource').map(r => r.responseEnd))"
        )
        now = page.evaluate("performance.now()")
        if now - last_end > 1000:
            break
        page.wait_for_timeout(200)
    m = page.evaluate(
        """() => {
        const fcp = (performance.getEntriesByName('first-contentful-paint')[0] || {}).startTime || 0;
        const res = performance.getEntriesByType('resource').filter(r => /\\.js(\\?|$)/.test(r.name))
          .map(r => ({ start: r.startTime, end: r.responseEnd, kb: (r.encodedBodySize || 0) / 1024 }));
        return {fcp, ui: window.__ui, lt: window.__lt, res,
                heap: performance.memory ? performance.memory.usedJSHeapSize : 0,
                lastRes: Math.max(0, ...performance.getEntriesByType('resource').map(r => r.responseEnd))};
    }"""
    )
    lt = m["lt"]
    ui = m["ui"] or m["fcp"]
    # critical = what the HTML itself pulls in (entry + its static imports): everything requested before the
    # entry chunk has arrived. Chunks fetched later come from dynamic import() (idle prefetch / on demand).
    entry_end = max(m["res"], key=lambda r: r["kb"])["end"]
    crit = [r for r in m["res"] if r["start"] < entry_end]
    blocking = lambda tasks: sum(max(0, d - 50) for s_, d in tasks)
    settled = max([m["lastRes"]] + [s_ + d for s_, d in lt])
    ctx.close()
    return {
        "fcp": m["fcp"],
        "ui": ui,
        "js_kb_critical": sum(r["kb"] for r in crit),
        "js_req_critical": len(crit),
        "tbt_to_ui": blocking([t for t in lt if t[0] < ui]),
        "js_kb_total": sum(r["kb"] for r in m["res"]),
        "js_req_total": len(m["res"]),
        "tbt_total": blocking(lt),
        "settled": settled,
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

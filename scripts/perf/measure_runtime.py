#!/usr/bin/env python3
"""Runtime benchmark: frame time while panning/zooming the 2D map and orbiting the 3D view, long
tasks, WebGL resource leaks and JS heap growth over repeated file switches.

Needs: an engine (python/server.py --port N), the perf build served somewhere
    npx vite build -c scripts/perf/vite.perf.config.ts && npx vite preview --outDir dist-perf --host 127.0.0.1 --port 4174
and a radar volume (and optionally a second file to alternate with).

    python scripts/perf/measure_runtime.py --url http://127.0.0.1:4174/ --engine-port 25611 \
        --file /path/KTLX20130520_201643_V06.gz --file2 /path/KGSP20140608_005103_V06.nc \
        --cycles 20 [--only frames|leak] [--json out.json]

Frame times come from a requestAnimationFrame sampler (interval between frames, so a stalled main
thread shows up as long intervals); long tasks from a PerformanceObserver. WebGL objects are
counted by wrapping create*/delete* on WebGL2RenderingContext (live = created - deleted).
"""
import argparse
import json
import statistics
import sys
import time

from playwright.sync_api import sync_playwright

INIT = r"""
(() => {
  window.__lt = [];
  new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__lt.push([e.startTime, e.duration]); })
    .observe({ type: 'longtask', buffered: true });

  // frame sampler
  window.__frames = null;
  window.__sampleStart = () => {
    window.__frames = []; window.__lt.length = 0;
    let last = performance.now();
    const tick = (t) => { if (!window.__frames) return; window.__frames.push(t - last); last = t; requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  };
  window.__sampleStop = () => { const f = window.__frames; window.__frames = null; return { frames: f, lt: window.__lt.slice() }; };

  // WebGL resource accounting. WeakRefs only: the instrumentation must not keep canvases or
  // contexts alive itself. After a forced GC "live" = created, not deleted, and still reachable.
  const kinds = ['Texture', 'Buffer', 'Program', 'Shader', 'Framebuffer', 'Renderbuffer', 'VertexArray'];
  const live = {}; const refOf = {}; const made = {}; let contexts = 0; const ctxRefs = [];
  for (const k of kinds) { live[k] = new Set(); refOf[k] = new WeakMap(); made[k] = 0; }
  window.__gl = () => ({
    contexts,
    contexts_alive: ctxRefs.filter((r) => r.deref() !== undefined).length,
    made: { ...made },
    live: Object.fromEntries(kinds.map((k) => [k, [...live[k]].filter((r) => r.deref() !== undefined).length])),
  });
  for (const proto of [WebGL2RenderingContext.prototype, WebGLRenderingContext.prototype]) {
    for (const k of kinds) {
      const c = proto['create' + k], d = proto['delete' + k];
      if (!c || !d) continue;
      proto['create' + k] = function (...a) { const o = c.apply(this, a); if (o) { const r = new WeakRef(o); live[k].add(r); refOf[k].set(o, r); made[k]++; } return o; };
      proto['delete' + k] = function (o) { if (o) { const r = refOf[k].get(o); if (r) live[k].delete(r); } return d.call(this, o); };
    }
  }
  const gc = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (t, ...a) { const r = gc.call(this, t, ...a); if (r && /webgl/.test(t) && !this.__counted) { this.__counted = true; contexts++; ctxRefs.push(new WeakRef(r)); } return r; };
})();
"""


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p / 100 * len(v)))]


def summarize(sample):
    f = sample["frames"][3:] or [0]
    lt = sample["lt"]
    return {
        "frames": len(f),
        "ft_p50_ms": round(pct(f, 50), 1),
        "ft_p95_ms": round(pct(f, 95), 1),
        "ft_max_ms": round(max(f), 1),
        "longtasks": len(lt),
        "longtask_total_ms": round(sum(d for _, d in lt), 0),
        "longtask_max_ms": round(max([d for _, d in lt] or [0]), 0),
    }


class Bench:
    def __init__(self, page, cdp, browser_cdp=None):
        self.page, self.cdp, self.browser_cdp = page, cdp, browser_cdp

    def skip_tour(self):
        try:
            self.page.get_by_text("Skip", exact=True).first.click(timeout=1500)
        except Exception:
            pass

    def metrics(self):
        return {m["name"]: m["value"] for m in self.cdp.send("Performance.getMetrics")["metrics"]}

    def sample(self, fn):
        """Run fn() while sampling frames/long tasks; returns the summary incl. main-thread busy %."""
        m0 = self.metrics()
        self.page.evaluate("window.__sampleStart()")
        fn()
        s = self.page.evaluate("window.__sampleStop()")
        m1 = self.metrics()
        r = summarize(s)
        wall = m1["Timestamp"] - m0["Timestamp"]
        r["main_busy_pct"] = round(100 * (m1["TaskDuration"] - m0["TaskDuration"]) / wall, 1)
        r["script_pct"] = round(100 * (m1["ScriptDuration"] - m0["ScriptDuration"]) / wall, 1)
        r["fps"] = round(r["frames"] / wall, 1)
        return r

    def gc(self):
        for _ in range(3):
            self.cdp.send("HeapProfiler.collectGarbage")

    def heap_mb(self):
        self.gc()
        self.page.wait_for_timeout(150)
        return self.cdp.send("Runtime.getHeapUsage")["usedSize"] / 1048576

    def rss_mb(self):
        """Resident memory of the page's renderer process (includes ArrayBuffers, which the JS heap figure does not)."""
        import subprocess

        info = self.browser_cdp.send("SystemInfo.getProcessInfo")["processInfo"]
        best = 0.0
        for pr in info:
            if pr["type"] != "renderer":
                continue
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(pr["id"])], capture_output=True, text=True).stdout.strip()
            if out:
                best = max(best, int(out) / 1024)
        return round(best, 0)

    def counters(self):
        c = self.cdp.send("Memory.getDOMCounters")
        return {"nodes": c["nodes"], "listeners": c["jsEventListeners"], "docs": c["documents"]}

    def open(self, path, timeout=90):
        n = self.page.evaluate("window.__xr.get(window.__xr.openFiles).length")
        # in-page timestamps: file registered in the store -> a viewer canvas on screen (covers the
        # on-demand load + mount of the viewer code when the viewer was not there yet)
        self.page.evaluate(
            """() => { window.__tf = null; window.__tc = null;
              const unsub = window.__xr.openFiles.subscribe((f) => { if (f.length > %d && window.__tf === null) window.__tf = performance.now(); });
              const mo = new MutationObserver(() => { if (window.__tf !== null && window.__tc === null && document.querySelector('.viewer-area canvas')) { window.__tc = performance.now(); mo.disconnect(); unsub(); } });
              mo.observe(document.body, { childList: true, subtree: true }); }""" % n
        )
        self.page.evaluate("(p) => window.__xr.wsManager.openFile(p)", path)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.page.evaluate("window.__xr.get(window.__xr.openFiles).length") > n:
                self.page.wait_for_timeout(1500)  # first sweep render
                tf, tc = self.page.evaluate("[window.__tf, window.__tc]")
                self.last_canvas_ms = None if (tf is None or tc is None) else round(tc - tf, 1)
                return time.time() - t0
            self.page.wait_for_timeout(100)
        raise RuntimeError("open timed out: " + path)

    def close_all(self):
        self.page.evaluate(
            "() => { for (const f of [...window.__xr.get(window.__xr.openFiles)]) { if (window.__xr.removeFile(f.id) !== null) window.__xr.wsManager.closeFile(f.id); } }"
        )
        self.page.wait_for_timeout(500)

    def view(self, v):
        label = {"2d": "2D", "3d": "3D", "dualpol": "DualPol"}[v]
        self.page.locator(".view-btn", has_text=label).first.click()
        self.page.wait_for_timeout(1500)

    def center(self):
        box = self.page.locator("canvas").first.bounding_box()
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def drag_loop(self, seconds, radius=120, wheel=False):
        cx, cy = self.center()
        import math

        t0 = time.time()
        i = 0
        self.page.mouse.move(cx, cy)
        self.page.mouse.down()
        while time.time() - t0 < seconds:
            a = i * 0.08
            self.page.mouse.move(cx + radius * math.cos(a), cy + radius * 0.6 * math.sin(a))
            if wheel and i % 25 == 0:
                self.page.mouse.wheel(0, -120 if (i // 25) % 2 == 0 else 120)
            i += 1
            self.page.wait_for_timeout(16)
        self.page.mouse.up()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4174/")
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--file2", default="")
    ap.add_argument("--cycles", type=int, default=20)
    ap.add_argument("--seconds", type=float, default=6)
    ap.add_argument("--only", choices=["frames", "leak"], default="")
    ap.add_argument("--cpu", type=float, default=1)
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist", "--enable-precise-memory-info", "--js-flags=--expose-gc"],
        )
        ctx = b.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.add_init_script(INIT)
        page.add_init_script(f"window.__XRADAR_PORT__ = {a.engine_port};")
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        cdp = ctx.new_cdp_session(page)
        cdp.send("HeapProfiler.enable")
        cdp.send("Performance.enable")
        cdp.send("Memory.getDOMCounters")
        if a.cpu > 1:
            cdp.send("Emulation.setCPUThrottlingRate", {"rate": a.cpu})
        page.goto(a.url)
        page.wait_for_function("window.__xr && window.__xr.wsManager.isConnected", timeout=30000)
        bench = Bench(page, cdp, b.new_browser_cdp_session())
        bench.skip_tour()

        out["open_s"] = round(bench.open(a.file), 2)
        out["file_to_canvas_ms"] = bench.last_canvas_ms
        if not a.only or a.only == "frames":
            page.wait_for_timeout(1500)
            bench.view("2d")
            out["idle_2d"] = bench.sample(lambda: page.wait_for_timeout(4000))
            out["pan_zoom_2d"] = bench.sample(lambda: bench.drag_loop(a.seconds, radius=150, wheel=True))
            bench.view("3d")
            page.wait_for_timeout(2500)
            out["idle_3d"] = bench.sample(lambda: page.wait_for_timeout(2000))
            out["orbit_3d"] = bench.sample(lambda: bench.drag_loop(a.seconds, radius=150))
            out["gl_after_3d"] = page.evaluate("window.__gl()")
            bench.view("2d")

        if not a.only or a.only == "leak":
            bench.close_all()
            page.wait_for_timeout(800)
            base = {"heap_mb": round(bench.heap_mb(), 1), "rss_mb": bench.rss_mb(), **bench.counters(), "gl": page.evaluate("window.__gl()")}
            series = []
            files = [a.file] + ([a.file2] if a.file2 else [])
            for i in range(a.cycles):
                f = files[i % len(files)]
                bench.open(f)
                n = page.evaluate("window.__xr.get(window.__xr.radarData).sweeps.length")
                for s in range(min(4, n)):
                    page.evaluate("(s) => window.__xr.selectedSweep.set(s)", s)
                    page.wait_for_timeout(500)
                if i % 2 == 0:
                    bench.view("3d")
                    page.wait_for_timeout(800)
                    bench.view("2d")
                bench.close_all()
                page.wait_for_timeout(500)
                series.append({"i": i + 1, "heap_mb": round(bench.heap_mb(), 1), "rss_mb": bench.rss_mb(), **bench.counters(), "gl": page.evaluate("window.__gl()")})
            out["leak"] = {"baseline": base, "series": series}
            first, last = series[1] if len(series) > 1 else series[0], series[-1]
            out["leak_summary"] = {
                "heap_baseline_mb": base["heap_mb"],
                "heap_after_cycle2_mb": first["heap_mb"],
                "heap_after_last_mb": last["heap_mb"],
                "rss_baseline_mb": base["rss_mb"],
                "rss_after_cycle2_mb": first["rss_mb"],
                "rss_after_last_mb": last["rss_mb"],
                "nodes_baseline": base["nodes"],
                "nodes_last": last["nodes"],
                "listeners_baseline": base["listeners"],
                "listeners_last": last["listeners"],
                "gl_live_baseline": base["gl"]["live"],
                "gl_live_last": last["gl"]["live"],
                "gl_contexts_created": last["gl"]["contexts"],
                "gl_contexts_alive": last["gl"]["contexts_alive"],
            }
        out["page_errors"] = errors[:5]
        b.close()
    print(json.dumps({k: v for k, v in out.items() if k != "leak"}, indent=1))
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    sys.exit(main())

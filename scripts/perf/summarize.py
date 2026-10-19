#!/usr/bin/env python3
"""Print a before/after table from the JSON written by run_all.sh:  summarize.py DIR base after"""
import json
import sys

d, a, b = sys.argv[1], sys.argv[2], sys.argv[3]


def load(label, name):
    try:
        return json.load(open(f"{d}/{label}_{name}.json"))
    except FileNotFoundError:
        return None


def row(name, x, y, fmt="{:.0f}"):
    if x is None or y is None:
        return
    print(f"{name:44s} {fmt.format(x):>10s} {fmt.format(y):>10s}")


print(f"{'':44s} {a:>10s} {b:>10s}")
for cpu in (1, 4):
    s1, s2 = load(a, f"startup_cpu{cpu}"), load(b, f"startup_cpu{cpu}")
    if s1 and s2:
        print(f"--- startup, CPU x{cpu} (median of {s1['summary']['runs']} cold loads)")
        for k, label in (("fcp", "first contentful paint ms"), ("ui", "usable UI painted ms"),
                         ("js_kb_critical", "JS before first paint (gzip KB)"), ("tbt_to_ui", "blocking time before UI ms"),
                         ("tbt_total", "blocking time, whole load ms"), ("settled", "everything settled ms")):
            row(label, s1["summary"][k]["median"], s2["summary"][k]["median"])
for cpu in (1, 4):
    f1, f2 = load(a, f"frames_cpu{cpu}"), load(b, f"frames_cpu{cpu}")
    if f1 and f2:
        print(f"--- frames, CPU x{cpu}")
        row("file registered -> viewer canvas ms", f1.get("file_to_canvas_ms"), f2.get("file_to_canvas_ms"), "{:.1f}")
        for sec in ("idle_2d", "pan_zoom_2d", "idle_3d", "orbit_3d"):
            for k, label in (("ft_p50_ms", "frame p50 ms"), ("ft_p95_ms", "frame p95 ms"), ("ft_max_ms", "frame max ms"),
                             ("longtasks", "long tasks"), ("main_busy_pct", "main thread busy %")):
                row(f"{sec}: {label}", f1[sec][k], f2[sec][k], "{:.1f}")
l1, l2 = load(a, "leak"), load(b, "leak")
if l1 and l2:
    print("--- 20 open / sweep / 3D / close cycles")
    for k in ("heap_baseline_mb", "heap_after_cycle2_mb", "heap_after_last_mb", "rss_baseline_mb", "rss_after_cycle2_mb",
              "rss_after_last_mb", "nodes_baseline", "nodes_last", "listeners_baseline", "listeners_last", "gl_contexts_alive"):
        row(k, l1["leak_summary"].get(k), l2["leak_summary"].get(k))
    for k in ("Buffer", "Texture", "Program", "Framebuffer", "VertexArray"):
        row("GL live " + k + " (after last cycle)", l1["leak_summary"]["gl_live_last"][k], l2["leak_summary"]["gl_live_last"][k])

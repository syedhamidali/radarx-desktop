#!/usr/bin/env python3
"""Find what keeps detached DOM nodes alive after repeated open/close cycles: take a V8 heap
snapshot and list, for the detached elements, who still points at them (retainer name + edge).

    python scripts/perf/detached.py --url http://127.0.0.1:4174/ --engine-port 25611 --file F [--file2 G] --cycles 6
"""
import argparse
import collections
import json
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from measure_runtime import INIT, Bench  # noqa: E402


def analyse(snap):
    meta = snap["snapshot"]["meta"]
    nf, ef = meta["node_fields"], meta["edge_fields"]
    nlen, elen = len(nf), len(ef)
    nodes, edges, strings = snap["nodes"], snap["edges"], snap["strings"]
    ntypes = meta["node_types"][0]
    etypes = meta["edge_types"][0]
    i_name, i_type, i_det, i_ec = nf.index("name"), nf.index("type"), nf.index("detachedness"), nf.index("edge_count")
    e_type, e_name, e_to = ef.index("type"), ef.index("name_or_index"), ef.index("to_node")
    n = len(nodes) // nlen
    detached = [nodes[k * nlen + i_det] == 2 for k in range(n)]
    names = [strings[nodes[k * nlen + i_name]] for k in range(n)]
    ntype = [ntypes[nodes[k * nlen + i_type]] for k in range(n)]
    cnt = collections.Counter(names[k] for k in range(n) if detached[k])
    inc = collections.defaultdict(collections.Counter)
    pos = 0
    for k in range(n):
        ec = nodes[k * nlen + i_ec]
        for _ in range(ec):
            t = etypes[edges[pos + e_type]]
            to = edges[pos + e_to] // nlen
            if detached[to] and not detached[k] and t not in ("weak",):
                nm = edges[pos + e_name]
                en = strings[nm] if t in ("property", "internal", "context", "shortcut", "element", "hidden") and isinstance(nm, int) and t != "element" else str(nm)
                inc[names[to]][(ntype[k], names[k][:60], t, en[:60])] += 1
            pos += elen
    # shortest retaining path from the GC roots to a few detached 3D-viewer nodes
    first_edge = [0] * (n + 1)
    acc = 0
    for k in range(n):
        first_edge[k] = acc
        acc += nodes[k * nlen + i_ec]
    first_edge[n] = acc
    parent = {0: None}
    q = collections.deque([0])
    while q:
        k = q.popleft()
        for ei in range(first_edge[k], first_edge[k + 1]):
            pos = ei * elen
            t = etypes[edges[pos + e_type]]
            if t == "weak":
                continue
            to = edges[pos + e_to] // nlen
            if to in parent:
                continue
            nm = edges[pos + e_name]
            en = strings[nm] if t not in ("element", "hidden") else str(nm)
            parent[to] = (k, f"{t}:{en[:40]}")
            q.append(to)
    paths = []
    want = [k for k in range(n) if detached[k] and "label-3d" in names[k]][:2]
    for k in want:
        chain = []
        cur = k
        while cur is not None and len(chain) < 40:
            par = parent.get(cur)
            chain.append(f"{names[cur][:50]} [{ntype[cur]}]" + (f" <-{par[1]}-" if par else ""))
            cur = par[0] if par else None
        paths.append(chain)
    return cnt, inc, paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:4174/")
    ap.add_argument("--engine-port", type=int, required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--file2", default="")
    ap.add_argument("--cycles", type=int, default=6)
    a = ap.parse_args()
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server", "--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist", "--js-flags=--expose-gc"])
        ctx = b.new_context(viewport={"width": 1500, "height": 950})
        page = ctx.new_page()
        page.add_init_script(INIT)
        page.add_init_script(f"window.__XRADAR_PORT__ = {a.engine_port};")
        cdp = ctx.new_cdp_session(page)
        page.goto(a.url)
        page.wait_for_function("window.__xr && window.__xr.wsManager.isConnected", timeout=30000)
        bench = Bench(page, cdp)
        bench.skip_tour()
        files = [a.file] + ([a.file2] if a.file2 else [])
        for i in range(a.cycles):
            bench.open(files[i % len(files)])
            bench.view("3d")
            bench.view("2d")
            bench.close_all()
        bench.gc()
        chunks = []
        cdp.on("HeapProfiler.addHeapSnapshotChunk", lambda e: chunks.append(e["chunk"]))
        cdp.send("HeapProfiler.takeHeapSnapshot", {"reportProgress": False})
        snap = json.loads("".join(chunks))
        cnt, inc, paths = analyse(snap)
        print("detached nodes by type:", cnt.most_common(15))
        for name, _ in cnt.most_common(6):
            print("\n==", name)
            for k, v in inc[name].most_common(6):
                print("  ", v, k)
        for ch in paths:
            print("\nretaining path (leaf first):")
            for c in ch:
                print("   ", c)
        b.close()


if __name__ == "__main__":
    main()

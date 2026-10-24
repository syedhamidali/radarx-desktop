#!/usr/bin/env python3
"""Engine start-up: time to the READY line, libraries mapped, memory (stdlib only).

    python scripts/perf/engine_startup.py                 # 5 runs, the python/.venv interpreter
    python scripts/perf/engine_startup.py --runs 9 --python /path/to/python --json out.json
    python scripts/perf/engine_startup.py --importtime    # which imports cost the most

Per run it starts ``python/server.py --port N`` in a fresh process (one at a time), waits for the
``READY:<port>`` line the shell waits for, then reads the number of native libraries the
process has mapped (``lsof`` on macOS and Linux; nothing on Windows) and its resident memory
(``ps``). It prints the median and the best of the runs. ``--after-s 2`` also looks again that
many seconds later, when the background pre-import (xradar, palettes, scipy) has finished.

Runs are not affected by the account gate: the engine is only started, nothing is requested.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY_DIR = ROOT / "python"
LIB = re.compile(r"\.(so(\.\d+)*|dylib|pyd|dll)\b")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def mapped_libraries(pid: int) -> int | None:
    if sys.platform == "win32":
        return None
    try:
        out = subprocess.run(
            ["lsof", "-p", str(pid)], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    paths = {line.split()[-1] for line in out.splitlines() if LIB.search(line)}
    return len(paths)


def rss_mb(pid: int) -> float | None:
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
        return round(int(out.stdout.strip()) / 1024, 1)
    except (OSError, ValueError):
        return None


def import_cpu_ms(python: str, py_dir: Path) -> tuple[float, float]:
    """``(cpu ms, wall ms)`` of ``import server`` in a fresh process (CPU is steadier when busy)."""
    code = (
        "import time; c = time.process_time(); w = time.perf_counter(); import server; "
        "print((time.process_time() - c) * 1000, (time.perf_counter() - w) * 1000)"
    )
    out = subprocess.run([python, "-c", code], cwd=py_dir, capture_output=True, text=True)
    cpu, wall = out.stdout.split()[-2:]
    return float(cpu), float(wall)


def one_run(python: str, after_s: float, py_dir: Path = PY_DIR) -> dict[str, float | int | None]:
    port = free_port()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [python, "server.py", "--port", str(port)],
        cwd=py_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if not line.startswith("READY"):
            raise RuntimeError(f"engine did not print READY (got {line!r})")
        ready_ms = (time.perf_counter() - t0) * 1000.0
        result: dict[str, float | int | None] = {
            "ready_ms": round(ready_ms, 1),
            "libraries_at_ready": mapped_libraries(proc.pid),
            "rss_mb_at_ready": rss_mb(proc.pid),
        }
        if after_s > 0:
            time.sleep(after_s)
            result["libraries_after"] = mapped_libraries(proc.pid)
            result["rss_mb_after"] = rss_mb(proc.pid)
        return result
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def import_time(python: str, top: int) -> None:
    proc = subprocess.run(
        [python, "-X", "importtime", "-c", "import server"],
        cwd=PY_DIR,
        capture_output=True,
        text=True,
    )
    rows = []
    for line in proc.stderr.splitlines():
        m = re.match(r"import time:\s+(\d+) \|\s+(\d+) \|(\s*)(\S+)", line)
        if m:
            rows.append((int(m.group(2)), len(m.group(3)), m.group(4)))
    print(f"{'cumulative ms':>14}  module (indented by import depth; -X importtime adds overhead)")
    for cum, depth, name in sorted(rows, reverse=True)[:top]:
        print(f"{cum / 1000:14.1f}  {' ' * (depth // 2)}{name}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--python", default=str(PY_DIR / ".venv" / "bin" / "python"))
    ap.add_argument("--after-s", type=float, default=0.0)
    ap.add_argument("--json", help="write the runs and medians to this file")
    ap.add_argument("--importtime", action="store_true", help="print the costliest imports")
    ap.add_argument(
        "--py-dir", type=Path, default=PY_DIR, help="another checkout's python/ folder (compare)"
    )
    ap.add_argument("--import-cpu", action="store_true", help="CPU time of `import server`")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()
    python = args.python if Path(args.python).exists() else sys.executable
    if args.importtime:
        import_time(python, args.top)
        return 0
    if args.import_cpu:
        times = [import_cpu_ms(python, args.py_dir) for _ in range(max(1, args.runs))]
        cpu, wall = [t[0] for t in times], [t[1] for t in times]
        print(f"import server in {args.py_dir} ({len(times)} runs)")
        print(f"  cpu ms  median {statistics.median(cpu):.0f}  min {min(cpu):.0f}")
        print(f"  wall ms median {statistics.median(wall):.0f}  min {min(wall):.0f}")
        return 0
    runs = [one_run(python, args.after_s, args.py_dir) for _ in range(max(1, args.runs))]
    summary: dict[str, float] = {}
    for key in runs[0]:
        values = [r[key] for r in runs if r.get(key) is not None]
        if values:
            summary[key + "_median"] = round(statistics.median(values), 1)
            summary[key + "_min"] = round(min(values), 1)
    print(f"{len(runs)} runs of {python}")
    for key, value in summary.items():
        print(f"  {key:32s} {value}")
    if args.json:
        Path(args.json).write_text(json.dumps({"runs": runs, "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

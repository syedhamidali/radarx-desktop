#!/usr/bin/env python3
"""Cheap benchmark regression check for CI (stdlib only; about a minute on a laptop).

    python scripts/perf/bench_check.py --python python/.venv/bin/python           # compare, exit 1 on regression
    python scripts/perf/bench_check.py --python python/.venv/bin/python --update  # rewrite the baseline

What it runs (synthetic volumes only, no radar data, no network, no account):

* engine start-up: the median of 3 starts to the ``READY`` line, and the libraries mapped then
  (``scripts/perf/engine_startup.py``);
* four engine stages of ``python/benchmarks/bench_engine.py`` (sweep switching, PNG render,
  3D volume, cross-section), measured in process CPU time, which does not care about a busy
  machine.

How it decides. ``python/benchmarks/ci_baseline.json`` holds the numbers of a reference run and
a calibration time (a fixed NumPy workload run by the same interpreter). The calibration of
*this* run divides out the speed of the machine, so a slow CI runner is not a regression. A
metric fails only when it is more than ``--factor`` (default 2.5) times its calibrated baseline
plus a small absolute allowance: this is meant to catch "something became several times
slower", not 10 % noise. Counts (libraries) get a tighter factor of 1.3.

The check cannot tell a regression from a different Python or library version, so refresh the
baseline (``--update``) in the same change that bumps them.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY_DIR = ROOT / "python"
BASELINE = PY_DIR / "benchmarks" / "ci_baseline.json"

#: (metric name, substring of the bench_engine row, absolute allowance in ms)
BENCH_ROWS = [
    ("sweep_switching_cpu_ms", "sweep switching: all 14 DBZH sweeps", 10.0),
    ("png_switch_cpu_ms", "PNG render: switch through 14 sweeps", 20.0),
    ("volume_res100_cpu_ms", "3D extract_volume DBZH res=100", 30.0),
    ("cross_section_cpu_ms", "cross-section DBZH", 10.0),
]

CALIBRATION = (
    "import time, numpy as np\n"
    "rng = np.random.default_rng(0)\n"
    "a = rng.random((400, 400)); b = rng.random(2_000_000)\n"
    "best = 1e9\n"
    "for _ in range(7):\n"
    "    t = time.process_time(); a @ a; np.sort(b); np.fft.rfft(b); "
    "best = min(best, time.process_time() - t)\n"
    "print(best * 1000)\n"
)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=PY_DIR, **kw)


def calibrate(python: str) -> float:
    out = run([python, "-c", CALIBRATION], timeout=120)
    if out.returncode != 0:
        raise SystemExit(f"calibration failed:\n{out.stderr[-1500:]}")
    return float(out.stdout.strip().splitlines()[-1])


def measure(python: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="bench-check-") as tmp:
        start_json = Path(tmp) / "start.json"
        out = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "perf" / "engine_startup.py"),
                "--runs",
                "3",
                "--python",
                python,
                "--json",
                str(start_json),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if out.returncode != 0:
            raise SystemExit(f"start-up measurement failed:\n{out.stdout}\n{out.stderr[-1500:]}")
        summary = json.loads(start_json.read_text())["summary"]
        metrics["ready_ms"] = summary["ready_ms_median"]
        if "libraries_at_ready_median" in summary:
            metrics["libraries_at_ready"] = summary["libraries_at_ready_median"]

        bench_json = Path(tmp) / "bench.json"
        out = run(
            [
                python,
                "benchmarks/bench_engine.py",
                "--only",
                "open",
                "sweeps",
                "render",
                "volume",
                "analysis",
                "--repeat",
                "3",
                "--no-memory",
                "--json",
                str(bench_json),
            ],
            timeout=900,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        if out.returncode != 0:
            raise SystemExit(f"bench_engine failed:\n{out.stdout[-800:]}\n{out.stderr[-1500:]}")
        rows = json.loads(bench_json.read_text())
    for name, needle, _ in BENCH_ROWS:
        row = next((r for r in rows if needle in r.get("what", "") and "cpu_ms" in r), None)
        if row is None:
            raise SystemExit(f"bench_engine produced no row matching {needle!r}")
        metrics[name] = round(row["cpu_ms"], 2)
    return metrics


def compare(
    metrics: dict[str, float], base: dict, calibration: float, factor: float
) -> list[tuple[str, float, float, float, bool]]:
    scale = calibration / base["calibration_ms"]
    allowance = {name: slack for name, _, slack in BENCH_ROWS}
    rows = []
    for name, value in metrics.items():
        if name not in base["metrics"]:
            continue
        ref = base["metrics"][name]
        if name == "libraries_at_ready":
            limit = ref * 1.3  # a count does not scale with machine speed
        else:
            limit = ref * scale * factor + allowance.get(name, 100.0 if name == "ready_ms" else 0)
        rows.append((name, ref, value, limit, value > limit))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--python", default=str(PY_DIR / ".venv" / "bin" / "python"))
    ap.add_argument("--factor", type=float, default=2.5)
    ap.add_argument("--update", action="store_true", help="write the baseline instead of checking")
    args = ap.parse_args()
    python = args.python if Path(args.python).exists() else sys.executable

    calibration = statistics.median(calibrate(python) for _ in range(3))
    metrics = measure(python)
    print(f"calibration {calibration:.1f} ms (machine speed reference)")

    if args.update:
        BASELINE.write_text(
            json.dumps(
                {
                    "note": "Reference run for scripts/perf/bench_check.py; refresh with --update "
                    "when Python or a library is upgraded.",
                    "python": run([python, "-c", "import sys; print(sys.version.split()[0])"])
                    .stdout.strip(),
                    "calibration_ms": round(calibration, 2),
                    "metrics": metrics,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"wrote {BASELINE}")
        for k, v in metrics.items():
            print(f"  {k:28s} {v}")
        return 0

    base = json.loads(BASELINE.read_text())
    rows = compare(metrics, base, calibration, args.factor)
    lines = [f"{'metric':28s} {'baseline':>10s} {'measured':>10s} {'limit':>10s}  result"]
    for name, ref, value, limit, bad in rows:
        lines.append(
            f"{name:28s} {ref:10.1f} {value:10.1f} {limit:10.1f}  {'REGRESSION' if bad else 'ok'}"
        )
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Benchmark check\n\n```\n{text}\n```\n")
    bad = [r[0] for r in rows if r[4]]
    if bad:
        print(f"\nSlower than {args.factor}x the calibrated baseline: {', '.join(bad)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

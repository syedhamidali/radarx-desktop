"""Stage 3b: the resource governor.

Decides HOW each step is processed for this data size and this machine, and states every
reduction. The coefficients are rough ESTIMATES (seconds per decoded MB, memory per volume) taken
from the measured runs in docs/PERFORMANCE.md and the data check timings; they are shown as such
("~") and recorded in the run record, never presented as measurements of this run.

* memory budget: ``--budget-gb`` or 60 % of physical RAM (same rule as the open-file budget,
  ``engine.reader.max_open_bytes``), minus what is open already;
* a step whose inputs do not fit at once is chunked (one volume at a time; mosaic / winds hold one
  time step = one volume per radar);
* long series are subsampled for the heavy products (movie, mosaic per step, winds per step) to
  ``MAX_STEPS`` time steps, evenly, and the reason is stated; cells/tracks and QPE keep every
  volume (they are light and need the full cadence);
* the plan refuses a step when even one time step does not fit the budget.
"""

from __future__ import annotations

import os
from typing import Any

# ESTIMATE coefficients (docs/PERFORMANCE.md: NEXRAD volume ~330 MB decoded; check ~1 s;
# HID ~3 s/sweep; mosaic ~4 s per radar per step; winds ~20 s per pair; QPE ~2 s per volume)
SEC_PER_DECODED_GB = {
    "check": 3.0,
    "qc": 25.0,
    "module": 40.0,
    "cells": 6.0,
    "mosaic": 12.0,
    "winds": 60.0,
    "qpe": 8.0,
    "plot": 10.0,
    "movie": 0.0,
}
MEM_FACTOR = {
    "check": 1.2,
    "qc": 2.5,
    "module": 2.2,
    "cells": 1.3,
    "mosaic": 1.6,
    "winds": 3.0,
    "qpe": 1.5,
    "plot": 1.3,
    "movie": 1.0,
}
MAX_STEPS = 24  # time steps for the per-step products
DEFAULT_RATIO = 3.5  # decoded bytes per byte on disk when no file was opened (NEXRAD gz)


def machine() -> dict[str, Any]:
    from engine.reader import system_memory_bytes

    total = system_memory_bytes()
    avail = None
    try:  # psutil is optional
        import psutil  # type: ignore

        avail = int(psutil.virtual_memory().available)
    except Exception:
        avail = None
    return {
        "ram_bytes": total,
        "available_bytes": avail,
        "cpus": os.cpu_count(),
        "budget_rule": "60 % of physical RAM unless --budget-gb is given",
    }


def budget_bytes(budget_gb: float | None, mach: dict[str, Any]) -> int:
    if budget_gb and budget_gb > 0:
        return int(budget_gb * 1024**3)
    total = mach.get("ram_bytes") or 8 * 1024**3
    b = int(total * 0.6)
    if mach.get("available_bytes"):
        b = min(b, int(mach["available_bytes"] * 0.9))
    return max(b, 512 * 1024**2)


def _decoded_bytes(inv: dict[str, Any], paths: list[str]) -> tuple[int, int]:
    """(decoded bytes of all ``paths``, decoded bytes of the largest single volume)."""
    by_path = {f["path"]: f for f in inv["files"]}
    total, largest = 0, 0
    for p in paths:
        f = by_path.get(p, {})
        radar = inv["radars"].get(f.get("radar") or "", {})
        ratio = radar.get("memory_ratio") or DEFAULT_RATIO
        dec = int(f.get("memory_bytes") or f.get("size_bytes", 0) * ratio)
        total += dec
        largest = max(largest, dec)
    return total, largest


def govern_step(step: dict[str, Any], inv: dict[str, Any], budget: int) -> dict[str, Any]:
    """Attach ``estimate``, ``reductions`` and ``refused`` to one step (in place; returned)."""
    kind = step["kind"]
    paths = list(step.get("inputs", []))
    radars = step.get("radars") or sorted(
        {f["radar"] for f in inv["files"] if f["path"] in set(paths) and f.get("radar")}
    )
    reductions: list[dict[str, Any]] = []
    total_dec, largest = _decoded_bytes(inv, paths)
    per_radar = (
        {
            r: [
                p
                for p in paths
                if inv["files"][[f["path"] for f in inv["files"]].index(p)].get("radar") == r
            ]
            for r in radars
        }
        if radars
        else {}
    )
    n_steps = max((len(v) for v in per_radar.values()), default=len(paths))

    # concurrency: how many volumes are in memory at once
    concurrent = (len(radars) or 2) if kind in ("mosaic", "winds") else 1
    mem_needed = int(largest * concurrent * MEM_FACTOR.get(kind, 1.5))
    if mem_needed > budget:
        step["refused"] = (
            f"one time step needs ~{mem_needed / 1e9:.1f} GB ({concurrent} volume(s) x "
            f"{MEM_FACTOR.get(kind, 1.5)}) "
            f"but the budget is {budget / 1e9:.1f} GB; raise --budget-gb or give smaller volumes"
        )
    # subsampling of time steps for per-step heavy products
    if kind in ("mosaic", "winds", "movie", "module") and n_steps > MAX_STEPS:
        every = -(-n_steps // MAX_STEPS)
        est_min = n_steps * SEC_PER_DECODED_GB.get(kind, 10) * largest * concurrent / 1e9 / 60
        reductions.append(
            {
                "what": f"time steps for {kind}",
                "from": n_steps,
                "to": -(-n_steps // every),
                "every": every,
                "reason": f"{n_steps} time steps would take ~{est_min:.0f} min; "
                f"every {every}. volume is used (evenly), "
                "the others are listed as skipped in the run record",
            }
        )
        n_eff = -(-n_steps // every)
    else:
        n_eff = n_steps
    processed_dec = (
        largest * concurrent * n_eff if kind in ("mosaic", "winds", "movie") else total_dec
    )
    seconds = SEC_PER_DECODED_GB.get(kind, 10.0) * processed_dec / 1e9
    if kind == "movie":
        seconds = 20.0 + 6.0 * n_eff  # rendering by Chrome, per frame step
    step["estimate"] = {
        "seconds": round(max(seconds, 1.0)),
        "memory_bytes": mem_needed,
        "volumes": len(paths),
        "time_steps": n_eff,
        "decoded_bytes": processed_dec,
        "kind": "ESTIMATE",
        "method": (
            "coefficients from docs/PERFORMANCE.md timings scaled by the decoded size "
            "of these files"
        ),
    }
    step["reductions"] = reductions
    step["chunking"] = (
        f"{concurrent} volume(s) in memory at a time; the rest stays on disk (lazy reading)"
    )
    return step

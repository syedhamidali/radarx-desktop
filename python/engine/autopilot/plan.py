"""Stage 3: the plan. Ranked, explained steps for exactly this data; refusals say what is missing."""

from __future__ import annotations

import time
from typing import Any

from engine.autopilot import govern
from engine.autopilot.rules import classify, evaluate_rules


def _rxd_command(step: dict[str, Any]) -> str:
    inputs = step.get("inputs", [])
    shown = " ".join(f'"{p}"' for p in inputs[:3]) + (
        f" ... ({len(inputs)} files)" if len(inputs) > 3 else ""
    )
    return step.get("rxd", "").replace("{inputs}", shown)


def make_plan(
    inv: dict[str, Any], *, budget_gb: float | None = None, out_dir: str | None = None
) -> dict[str, Any]:
    """The plan for ``inv`` (from :func:`engine.autopilot.inspect_inputs`). JSON-able."""
    t0 = time.perf_counter()
    mach = govern.machine()
    budget = govern.budget_bytes(budget_gb, mach)
    situation = classify(inv)
    table = evaluate_rules(inv)
    steps: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    notes: list[str] = []
    for row in table:
        if row.get("step"):
            step = govern.govern_step(dict(row["step"]), inv, budget)
            step["rxd_command"] = _rxd_command(step)
            step["enabled"] = not step.get("refused") and not step.get("manual")
            step["evidence"] = row["evidence"]
            steps.append(step)
        if row.get("refusal"):
            refusals.append({**row["refusal"], "rule": row["id"], "evidence": row["evidence"]})
        if row.get("note"):
            notes.append(row["note"])
    # rank: rule rank first (the dependency order), then weight; refused ones last
    steps.sort(
        key=lambda s: (
            bool(s.get("refused")),
            next(r["rank"] for r in table if r["id"] == s["rule"]),
        )
    )
    for n, s in enumerate(steps, 1):
        s["order"] = n
    total_s = sum(s["estimate"]["seconds"] for s in steps if s["enabled"])
    peak = max((s["estimate"]["memory_bytes"] for s in steps if s["enabled"]), default=0)
    reductions = list(inv.get("reductions", [])) + [
        r for s in steps for r in s.get("reductions", [])
    ]
    return {
        "version": 1,
        "situation": situation,
        "rules": table,
        "steps": steps,
        "refusals": refusals,
        "notes": notes,
        "governor": {
            "machine": mach,
            "budget_bytes": budget,
            "total_seconds_estimate": round(total_s),
            "peak_memory_bytes_estimate": peak,
            "reductions": reductions,
            "note": "Estimates are rough (ESTIMATE); every reduction above is repeated in the run record and the outputs' provenance.",
        },
        "out_dir": out_dir,
        "consent": "Nothing runs, is downloaded or written until the steps are approved.",
        "elapsed_ms": round(1000 * (time.perf_counter() - t0)),
    }


def format_plan(plan: dict[str, Any], inv: dict[str, Any] | None = None) -> str:
    """Plain-text plan for the terminal."""
    lines = [f"Situation: {plan['situation']['label']}"]
    lines += [f"  {e}" for e in plan["situation"]["evidence"]]
    g = plan["governor"]
    lines.append(
        f"Budget: {g['budget_bytes'] / 1e9:.1f} GB of {(g['machine'].get('ram_bytes') or 0) / 1e9:.0f} GB RAM; "
        f"~{g['total_seconds_estimate'] / 60:.1f} min for the enabled steps (ESTIMATE)"
    )
    lines.append("")
    lines.append("Plan:")
    for s in plan["steps"]:
        flag = "x" if s["enabled"] else ("-" if s.get("refused") else "m")
        est = s["estimate"]
        lines.append(
            f"  [{flag}] {s['order']}. {s['title']}  (~{est['seconds']} s, ~{est['memory_bytes'] / 1e9:.1f} GB, {est['volumes']} volumes)"
        )
        lines.append(f"       why: {s['why']}")
        lines.append(f"       needs: {s['needs']}")
        lines.append(f"       produces: {s['produces']}  [{s['provenance']}]")
        for r in s.get("reductions", []):
            lines.append(f"       reduced: {r['what']} {r['from']} -> {r['to']}: {r['reason']}")
        if s.get("refused"):
            lines.append(f"       refused: {s['refused']}")
        if s.get("manual"):
            lines.append("       manual: not run here; use the command")
        lines.append(f"       rxd: {s['rxd_command']}")
    if plan["refusals"]:
        lines.append("")
        lines.append("Not possible with this data:")
        for r in plan["refusals"]:
            lines.append(f"  - {r['what']}: missing {r['missing']}")
    if plan["notes"]:
        lines.append("")
        lines.append("Be aware:")
        lines += [f"  - {n}" for n in plan["notes"]]
    lines.append("")
    lines.append("Rules (evidence):")
    for r in plan["rules"]:
        lines.append(f"  {'fired ' if r['fired'] else 'quiet '} {r['id']}: {r['title']}")
        lines += [f"      {e}" for e in r["evidence"]]
    return "\n".join(lines)

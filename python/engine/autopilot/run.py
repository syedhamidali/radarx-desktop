"""Stage 4: execute the approved steps with the existing engine functions.

* each step kind maps to one engine call (``engine.api``: pipeline / run / cells / track / mosaic /
  winds / qpe / plot); the inputs are opened one time step at a time and closed again;
* ``out_dir/autopilot_state.json`` records finished steps, so a re-run skips them (resumable);
* ``out_dir/run_record.json`` holds the inventory, the plan, every decision with its reason, the
  reductions, the exact ``rxd`` commands, software and library versions, parameters and timings;
* ``out_dir/SUMMARY.md`` is the one-page summary;
* ``cancel()`` is polled between volumes and passed to the engine functions that accept it.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from engine.autopilot.inspect import AutopilotError

logger = logging.getLogger(__name__)
ProgressFn = Callable[[int, str], None]


def _stamp(path: str, inv: dict[str, Any]) -> str:
    f = next((x for x in inv["files"] if x["path"] == path), {})
    t = (f.get("time") or "").replace("-", "").replace(":", "")[:15]
    return f"{f.get('radar', 'RADAR')}_{t or Path(path).stem}"


def _by_radar(step: dict[str, Any], inv: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    by_path = {f["path"]: f for f in inv["files"]}
    for p in step.get("inputs", []):
        out.setdefault(by_path.get(p, {}).get("radar") or "RADAR", []).append(p)
    for v in out.values():
        v.sort(key=lambda p: by_path.get(p, {}).get("time") or p)
    return out


def _subsample(paths: list[str], step: dict[str, Any]) -> tuple[list[str], list[str]]:
    every = next((r["every"] for r in step.get("reductions", []) if r.get("every")), 1)
    if every <= 1:
        return paths, []
    keep = paths[::every]
    return keep, [p for p in paths if p not in set(keep)]


def _time_steps(step: dict[str, Any], inv: dict[str, Any]) -> list[dict[str, str]]:
    """For multi-radar steps: ``[{radar: path}]`` at the reference radar's times (nearest others)."""
    from engine.animate_plan import parse_time

    groups = _by_radar(step, inv)
    by_path = {f["path"]: f for f in inv["files"]}
    ref = max(groups, key=lambda r: len(groups[r]))
    steps: list[dict[str, str]] = []
    for p in groups[ref]:
        t = by_path[p].get("time")
        row = {ref: p}
        for r, paths in groups.items():
            if r == ref:
                continue
            with_t = [(abs((parse_time(by_path[q]["time"]) - parse_time(t)).total_seconds()), q) for q in paths if by_path[q].get("time")] if t else []
            if with_t:
                d, q = min(with_t)
                if d <= 600:
                    row[r] = q
        if len(row) >= 2:
            steps.append(row)
    return steps


def _run_step(step: dict[str, Any], inv: dict[str, Any], out: Path, progress: ProgressFn | None, cancel: Callable[[], bool] | None) -> dict[str, Any]:
    from engine import api

    kind = step["kind"]
    files_written: list[str] = []
    notes: list[str] = []
    params: dict[str, Any] = {}

    def check_cancel() -> None:
        if cancel is not None and cancel():
            raise AutopilotError("cancelled")

    def say(pct: int, text: str) -> None:
        if progress:
            progress(pct, text)

    if kind == "check":
        from engine import quality

        for n, p in enumerate(step["inputs"]):
            check_cancel()
            say(int(100 * n / len(step["inputs"])), f"Data check {Path(p).name}")
            with api.open(p) as vol:
                rep = quality.check_volume(vol)
            target = out / f"check_{_stamp(p, inv)}.json"
            target.write_text(json.dumps(rep, indent=1, default=str))
            files_written.append(str(target))
    elif kind == "qc":
        recipes = step.get("recipes", {})
        groups = _by_radar(step, inv)
        todo = [(r, p) for r, ps in groups.items() for p in ps if r in recipes]
        for n, (r, p) in enumerate(todo):
            check_cancel()
            say(int(100 * n / max(1, len(todo))), f"QC {Path(p).name}")
            with api.open(p) as vol:
                for recipe in recipes[r]:
                    vol.pipeline(recipe, all_sweeps=True)
                    params.setdefault(r, []).append(recipe.get("name"))
                target = out / f"{_stamp(p, inv)}_qc.nc"
                vol.export("nc", out=target)
            files_written.append(str(target))
    elif kind == "module":
        for n, p in enumerate(step["inputs"]):
            check_cancel()
            say(int(100 * n / len(step["inputs"])), f"{step['module']} {Path(p).name}")
            with api.open(p) as vol:
                target = out / f"{_stamp(p, inv)}_{step['module']}.nc"
                plot = out / f"{_stamp(p, inv)}_{step['module']}.png"
                vol.run(step["module"], out=target, plot=plot)
            files_written += [str(target), str(plot)]
    elif kind == "cells":
        groups = _by_radar(step, inv)
        for n, (r, paths) in enumerate(groups.items()):
            check_cancel()
            say(int(100 * n / len(groups)), f"Cells {r}")
            if len(paths) > 1:
                target = out / f"tracks_{r}.csv"
                plot = out / f"tracks_{r}.png"
                api.track(paths, out=target, plot=plot)
            else:
                target = out / f"cells_{r}.csv"
                plot = out / f"cells_{r}.png"
                api.cells(paths[0], out=target, plot=plot)
            files_written += [str(target), str(plot)]
        params["threshold_dbz"] = 35.0
    elif kind in ("mosaic", "winds"):
        steps_t = _time_steps(step, inv)
        kept, skipped = _subsample([json.dumps(s, sort_keys=True) for s in steps_t], step)
        if skipped:
            notes.append(f"{len(skipped)} time step(s) skipped by the governor (every {next((r['every'] for r in step.get('reductions', []) if r.get('every')), 1)}.)")
        for n, raw in enumerate(kept):
            check_cancel()
            row = json.loads(raw)
            paths = list(row.values())
            label = _stamp(paths[0], inv).split("_", 1)[-1]
            say(int(100 * n / max(1, len(kept))), f"{kind} {label} ({', '.join(row)})")
            target = out / f"{kind}_{label}.nc"
            plot = out / f"{kind}_{label}.png"
            if kind == "mosaic":
                api.mosaic(paths, out=target, plot=plot, progress=lambda a, b: None)
            else:
                api.winds(paths, out=target, plot=plot, cancel=cancel)
            files_written += [str(target), str(plot)]
    elif kind == "qpe":
        groups = _by_radar(step, inv)
        for n, (r, paths) in enumerate(groups.items()):
            check_cancel()
            say(int(100 * n / len(groups)), f"Rainfall {r}")
            band = inv["radars"].get(r, {}).get("band") or "S"
            target = out / f"qpe_{r}.nc"
            plot = out / f"qpe_{r}.png"
            api.qpe(paths, band=band, out=target, plot=plot)
            params[r] = {"band": band, "method": "auto"}
            files_written += [str(target), str(plot)]
    elif kind == "plot":
        for n, p in enumerate(step["inputs"]):
            check_cancel()
            say(int(100 * n / len(step["inputs"])), f"Plot {Path(p).name}")
            plot = out / f"rhi_{_stamp(p, inv)}.png"
            api.plot(p, out=plot)
            files_written.append(str(plot))
    elif kind == "movie":
        notes.append("not run here (needs Chrome + PyAV); command: " + step["rxd_command"])
    else:
        raise AutopilotError(f"unknown step kind {kind!r}")
    return {"files": files_written, "notes": notes, "params": params}


def _summary(record: dict[str, Any]) -> str:
    plan = record["plan"]
    lines = ["# Autopilot summary", "", f"Run at {record['started']} (UTC) with {record['software']}.", "", f"**Situation:** {plan['situation']['label']}", ""]
    lines += [f"- {e}" for e in plan["situation"]["evidence"]]
    lines += ["", "## Steps", ""]
    for s in record["steps"]:
        status = s["status"]
        lines.append(f"- **{s['title']}** - {status}" + (f" ({s['elapsed_s']:.0f} s)" if s.get("elapsed_s") else ""))
        lines.append(f"  - why: {s['why']}")
        lines.append(f"  - provenance: {s['provenance']}")
        if s.get("files"):
            lines.append("  - wrote: " + ", ".join(Path(f).name for f in s["files"]))
        for n in s.get("notes", []):
            lines.append(f"  - note: {n}")
        if s.get("error"):
            lines.append(f"  - error: {s['error']}")
        lines.append(f"  - command: `{s['rxd_command']}`")
    if plan["refusals"]:
        lines += ["", "## Not possible with this data", ""]
        lines += [f"- {r['what']}: missing {r['missing']}" for r in plan["refusals"]]
    reds = plan["governor"]["reductions"]
    if reds:
        lines += ["", "## Reductions decided by the governor", ""]
        lines += [f"- {r['what']}: {r['from']} -> {r['to']}: {r['reason']}" for r in reds]
    lines += ["", "## Decisions", ""]
    for r in plan["rules"]:
        lines.append(f"- {r['id']} ({'fired' if r['fired'] else 'quiet'}): " + "; ".join(r["evidence"][:3]))
    lines += ["", "Estimates were rough (ESTIMATE); times above are measured. Nothing was downloaded."]
    return "\n".join(lines) + "\n"


def run_plan(
    inv: dict[str, Any],
    plan: dict[str, Any],
    *,
    out_dir: str | Path,
    step_ids: list[str] | None = None,
    progress: ProgressFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run the approved ``step_ids`` (default: the enabled ones) and write the run record."""
    from engine import provenance

    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "autopilot_state.json"
    state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text())
        except ValueError:
            state = {}
    wanted = set(step_ids) if step_ids is not None else {s["id"] for s in plan["steps"] if s["enabled"]}
    chosen = [s for s in plan["steps"] if s["id"] in wanted]
    unknown = wanted - {s["id"] for s in chosen}
    if unknown:
        raise AutopilotError(f"unknown step(s): {', '.join(sorted(unknown))}")
    record: dict[str, Any] = {
        "version": 1,
        "started": datetime.utcnow().isoformat(timespec="seconds"),
        "software": provenance.software_string(),
        "libraries": provenance.library_versions(),
        "inventory": {k: v for k, v in inv.items() if k != "files"},
        "n_files": inv["n_files"],
        "inputs": [f["path"] for f in inv["files"]],
        "plan": plan,
        "approved": sorted(wanted),
        "steps": [],
    }
    n = len(chosen)
    for i, s in enumerate(chosen):
        entry = {k: s.get(k) for k in ("id", "title", "why", "provenance", "rxd_command", "reductions", "estimate")}
        if s.get("refused"):
            entry.update(status="refused", error=s["refused"])
            record["steps"].append(entry)
            continue
        if state.get(s["id"], {}).get("status") == "done":
            entry.update(status="skipped (done earlier)", files=state[s["id"]].get("files", []))
            record["steps"].append(entry)
            continue
        base = int(100 * i / max(1, n))
        span = int(100 / max(1, n))
        sub = (lambda pct, text: progress(base + pct * span // 100, f"{i + 1}/{n} {text}")) if progress else None
        t0 = time.perf_counter()
        try:
            res = _run_step(s, inv, out, sub, cancel)
            entry.update(status="done", elapsed_s=round(time.perf_counter() - t0, 1), **res)
            state[s["id"]] = {"status": "done", "files": res["files"], "when": record["started"]}
        except AutopilotError as exc:
            entry.update(status="cancelled" if "cancel" in str(exc) else "failed", error=str(exc), elapsed_s=round(time.perf_counter() - t0, 1))
            record["steps"].append(entry)
            if "cancel" in str(exc):
                break
            continue
        except Exception as exc:
            logger.warning("autopilot step %s failed: %s\n%s", s["id"], exc, traceback.format_exc())
            entry.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}", elapsed_s=round(time.perf_counter() - t0, 1))
        record["steps"].append(entry)
        state_path.write_text(json.dumps(state, indent=1))
    record["finished"] = datetime.utcnow().isoformat(timespec="seconds")
    (out / "run_record.json").write_text(json.dumps(record, indent=1, default=str))
    (out / "SUMMARY.md").write_text(_summary(record))
    record["out_dir"] = str(out)
    record["summary_path"] = str(out / "SUMMARY.md")
    record["record_path"] = str(out / "run_record.json")
    if progress:
        progress(100, "Autopilot finished")
    return record

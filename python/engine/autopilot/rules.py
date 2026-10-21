"""Stage 2: the situation and the rule table.

A rule is a plain function ``rule(inv) -> Outcome``; ``@rule(id, title)`` registers it in
:data:`RULES`. :func:`evaluate_rules` runs every rule and returns the table (id, title, fired,
evidence lines, the proposed step or refusal). Nothing here is a black box: the evidence of each
rule is a list of sentences built from the inventory numbers.

To add a rule: write one function below with the decorator. It may propose a *step* (an analysis
the planner estimates and ranks), a *refusal* (what is missing for an analysis the data invites
but cannot support) or a *note* (a problem to be aware of). Steps carry the provenance class of
their output (docs/SCIENCE.md) and the equivalent ``rxd`` command template.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

Inventory = dict[str, Any]


@dataclass
class Outcome:
    fired: bool
    evidence: list[str] = field(default_factory=list)
    step: dict[str, Any] | None = None
    refusal: dict[str, Any] | None = None
    note: str | None = None


@dataclass
class Rule:
    id: str
    title: str
    fn: Callable[[Inventory], Outcome]
    rank: int  # lower runs first in the plan


RULES: list[Rule] = []


def rule(rule_id: str, title: str, rank: int = 50) -> Callable[[Callable[[Inventory], Outcome]], Callable[[Inventory], Outcome]]:
    def deco(fn: Callable[[Inventory], Outcome]) -> Callable[[Inventory], Outcome]:
        RULES.append(Rule(rule_id, title, fn, rank))
        return fn

    return deco


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(inv: Inventory) -> dict[str, Any]:
    """``{kind, label, evidence}``: single_snapshot | time_series | network | network_series | rhi
    | mixed | unknown."""
    radars = {n: r for n, r in inv["radars"].items() if n != "UNKNOWN"}
    ev: list[str] = []
    n_r = len(radars)
    series = [r for r in radars.values() if r["n_files"] > 1]
    rhi = [r for r in radars.values() if r["rhi"]]
    ppi = [r for r in radars.values() if "ppi" in r["kinds"]]
    ev.append(f"{inv['n_files']} file(s), {n_r} radar(s): {', '.join(sorted(radars)) or 'none identified'}")
    for r in radars.values():
        ev.append(
            f"{r['name']}: {r['n_files']} volume(s)"
            + (f" from {r['t0']} to {r['t1']}" if r['t1'] and r['t1'] != r['t0'] else f" at {r['t0']}" if r['t0'] else "")
            + (f", cadence {r['cadence_s']} s" if r['cadence_s'] else "")
            + (f", {len(r['gaps'])} gap(s)" if r['gaps'] else "")
        )
    if n_r == 0:
        kind = "unknown"
    elif rhi and not ppi:
        kind = "rhi"
    elif rhi and ppi:
        kind = "mixed"
    elif n_r == 1 and not series:
        kind = "single_snapshot"
    elif n_r == 1:
        kind = "time_series"
    elif n_r > 1 and not series:
        kind = "network"
    else:
        kind = "network_series"
    labels = {
        "unknown": "No radar volume identified",
        "rhi": "RHI scans",
        "mixed": "Mixed PPI volumes and RHI scans",
        "single_snapshot": "One radar, one volume",
        "time_series": "Time series of one radar",
        "network": "Several radars, one time",
        "network_series": "Several radars over time",
    }
    return {"kind": kind, "label": labels[kind], "evidence": ev}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _radars(inv: Inventory) -> dict[str, dict[str, Any]]:
    return {n: r for n, r in inv["radars"].items() if n != "UNKNOWN"}


def _files_of(inv: Inventory, radar: str) -> list[str]:
    return [f["path"] for f in inv["files"] if f.get("radar") == radar and "error" not in f]


def _check_findings(inv: Inventory, radar: str, severity: tuple[str, ...] = ("warning", "notice")) -> list[dict[str, Any]]:
    rep = inv.get("checks", {}).get(radar) or {}
    return [f for f in rep.get("findings", []) if f["severity"] in severity]


# ---------------------------------------------------------------------------
# Rules (order of definition is not the plan order; ``rank`` is)
# ---------------------------------------------------------------------------


@rule("inventory", "Inventory and data check", rank=0)
def _r_inventory(inv: Inventory) -> Outcome:
    ev = [f"{inv['n_files']} file(s), {inv['total_bytes'] / 1e6:.0f} MB on disk, {inv['n_opened']} opened for metadata"]
    for name, rep in inv.get("checks", {}).items():
        if "error" in rep:
            ev.append(f"{name}: data check failed ({rep['error']})")
            continue
        c = rep.get("counts") or {}
        ev.append(f"{name}: data check {c.get('warning', 0)} warning(s), {c.get('notice', 0)} notice(s)")
    if inv.get("unreadable"):
        ev.append(f"{len(inv['unreadable'])} file(s) could not be opened: " + "; ".join(u["name"] for u in inv["unreadable"][:5]))
    return Outcome(
        True,
        ev,
        step={
            "id": "check",
            "kind": "check",
            "title": "Data check report of every radar",
            "why": "Know the problems (aliasing, clutter, noise, calibration offset) before any product is made.",
            "needs": "one volume per radar (already inspected; the run writes the full reports)",
            "produces": "check_<radar>.json, one report per radar",
            "provenance": "RETRIEVED (measured in the data; estimates are labelled in each finding)",
            "inputs": [r["representative"] for r in _radars(inv).values() if r["representative"]],
            "rxd": "rxd check -i {inputs} --json",
            "weight": 0.3,
        },
    )


@rule("qc", "QC recipe from the data check", rank=10)
def _r_qc(inv: Inventory) -> Outcome:
    ev: list[str] = []
    recipes: dict[str, list[dict[str, Any]]] = {}
    for name, rep in inv.get("checks", {}).items():
        items = rep.get("recipes") or []
        if items:
            recipes[name] = items
            ev.append(f"{name}: the data check suggests " + "; ".join(f"{it['role']}: {', '.join(s['step_id'] for s in it['recipe']['steps'])}" for it in items))
        else:
            ev.append(f"{name}: no clean-up step suggested by the data check")
    if not recipes:
        return Outcome(False, ev or ["no data check available"])
    return Outcome(
        True,
        ev,
        step={
            "id": "qc",
            "kind": "qc",
            "title": "Clean-up (QC recipe suggested by the data check)",
            "why": "Each step is tied to a finding (de-alias when folds were counted, clutter filter when ground echo was found, ...).",
            "needs": "the suggested recipe per radar; one volume at a time",
            "produces": "cleaned fields as derived variables in <radar>_<time>_qc.nc, with the recipe in the history",
            "provenance": "RETRIEVED (algorithm per step, parameters recorded)",
            "inputs": [p for n in recipes for p in _files_of(inv, n)],
            "recipes": {n: [it["recipe"] for it in items] for n, items in recipes.items()},
            "rxd": "rxd check -i FILE --fix-plan -o recipe.json && rxd pipeline recipe.json -i FILE",
            "weight": 1.0,
        },
    )


@rule("dualpol", "Hydrometeor classification (dual-pol)", rank=20)
def _r_hid(inv: Inventory) -> Outcome:
    ev, ok = [], []
    for r in _radars(inv).values():
        if r["dual_pol"]:
            ev.append(f"{r['name']}: ZDR, RhoHV, PhiDP present ({', '.join(r['roles'][k] for k in ('zdr', 'rhohv', 'phidp'))}), band {r['band'] or 'not stated'}")
            ok.append(r["name"])
        else:
            missing = [k for k in ("zdr", "rhohv", "phidp") if k not in r["roles"]]
            ev.append(f"{r['name']}: not dual-pol ({', '.join(missing)} missing)")
    if not ok:
        return Outcome(False, ev, refusal={"what": "hydrometeor classification", "missing": "dual-polarisation fields (ZDR, RhoHV, PhiDP)"})
    return Outcome(
        True,
        ev,
        step={
            "id": "hid",
            "kind": "module",
            "module": "radarx_hid",
            "title": "Hydrometeor classification",
            "why": "Dual-pol fields are present: a fuzzy-logic classification tells rain, hail, graupel, snow and biological echo apart.",
            "needs": "radarx; the band (used for the membership functions); the lowest sweep is classified by default",
            "produces": "HID field per volume (<radar>_<time>_hid.nc)",
            "provenance": "RETRIEVED (fuzzy-logic HID, radarx; limits: band assumed when not stated, melting layer from the data)",
            "inputs": [p for n in ok for p in _files_of(inv, n)],
            "rxd": "rxd run radarx_hid -i {inputs}",
            "weight": 2.0,
        },
    )


@rule("cells", "Storm cells and tracks", rank=30)
def _r_cells(inv: Inventory) -> Outcome:
    ev, series, single = [], [], []
    for r in _radars(inv).values():
        if not r["has_reflectivity"] or r["rhi"] and "ppi" not in r["kinds"]:
            ev.append(f"{r['name']}: no PPI reflectivity")
            continue
        if r["n_files"] > 1:
            series.append(r["name"])
            ev.append(f"{r['name']}: {r['n_files']} volumes over {r['span_s'] / 60:.0f} min: cells can be linked in time")
        else:
            single.append(r["name"])
            ev.append(f"{r['name']}: one volume: cells can be identified, not tracked")
    if not (series or single):
        return Outcome(False, ev, refusal={"what": "storm cells", "missing": "PPI reflectivity"})
    return Outcome(
        True,
        ev,
        step={
            "id": "cells",
            "kind": "cells",
            "title": "Storm cells" + (" and tracks" if series else ""),
            "why": "Reflectivity above 35 dBZ grouped into cells gives size, intensity, motion" + (" and tracks across the series" if series else "") + ".",
            "needs": "reflectivity on the lowest sweep; a series of one radar for tracks",
            "produces": "cells_<radar>.csv (+ tracks_<radar>.csv with motion vectors)",
            "provenance": "RETRIEVED (threshold segmentation; tracks by overlap and distance)",
            "inputs": [p for n in series + single for p in _files_of(inv, n)],
            "radars_series": series,
            "radars_single": single,
            "rxd": "rxd cells -i {inputs}  # several files of one radar: tracks",
            "weight": 0.6,
        },
    )


@rule("mosaic", "Mosaic of overlapping radars", rank=40)
def _r_mosaic(inv: Inventory) -> Outcome:
    ev, good = [], []
    for p in inv.get("pairs", []):
        if p["overlap_space"] and (p["time_overlap_s"] is None or p["time_overlap_s"] >= 0):
            good.append(p)
            ev.append(f"{p['a']}-{p['b']}: {p['distance_km']} km apart, coverage overlaps, time overlap " + ("unknown" if p["time_overlap_s"] is None else f"{p['time_overlap_s'] / 60:.0f} min"))
        elif p["overlap_space"]:
            ev.append(f"{p['a']}-{p['b']}: coverage overlaps but the times are {-p['time_overlap_s'] / 60:.0f} min apart")
        else:
            ev.append(f"{p['a']}-{p['b']}: {p['distance_km']} km apart, no coverage overlap")
    if not inv.get("pairs"):
        return Outcome(False, ["one radar (or no site positions): nothing to merge"], refusal={"what": "mosaic", "missing": "a second radar whose coverage overlaps in space and time"})
    if not good:
        return Outcome(False, ev, refusal={"what": "mosaic", "missing": "radars that overlap in space AND time"})
    radars = sorted({p["a"] for p in good} | {p["b"] for p in good})
    return Outcome(
        True,
        ev,
        step={
            "id": "mosaic",
            "kind": "mosaic",
            "title": "Reflectivity mosaic (composite) of " + ", ".join(radars),
            "why": "Overlapping radars at the same time: one map of the column maximum, with calibration offsets between radars estimated in the overlap.",
            "needs": "radarx; the volumes nearest to one common time per step",
            "produces": "mosaic_<time>.nc + .png per time step",
            "provenance": "RETRIEVED (beam-weighted merge on a 1 km grid; offsets between radars are estimated, not measured)",
            "inputs": [p for n in radars for p in _files_of(inv, n)],
            "radars": radars,
            "rxd": "rxd mosaic -i {inputs} -o mosaic.nc -p mosaic.png",
            "weight": 1.5,
        },
    )


@rule("winds", "Multi-Doppler winds", rank=45)
def _r_winds(inv: Inventory) -> Outcome:
    ev, good = [], []
    for p in inv.get("pairs", []):
        g = p.get("dual_doppler")
        if not p["both_velocity"]:
            ev.append(f"{p['a']}-{p['b']}: velocity missing on one radar")
            continue
        if g is None:
            ev.append(f"{p['a']}-{p['b']}: no coverage overlap for dual Doppler")
            continue
        ev.append(f"{p['a']}-{p['b']}: beam crossing >= 30 deg on {g['good_fraction'] * 100:.0f}% of the dual-coverage area ({g['good_crossing_km2']:,} km2; {g['kind']})")
        if g["good_fraction"] >= 0.2 and (p["time_overlap_s"] is None or p["time_overlap_s"] >= 0):
            good.append(p)
    if not inv.get("pairs"):
        return Outcome(False, ["one radar: no dual-Doppler geometry"], refusal={"what": "multi-Doppler winds", "missing": "a second radar with Doppler velocity 30-150 km away"})
    if not good:
        return Outcome(False, ev, refusal={"what": "multi-Doppler winds", "missing": "two radars with velocity, overlapping in time, with enough beam-crossing area"})
    radars = sorted({p["a"] for p in good} | {p["b"] for p in good})
    return Outcome(
        True,
        ev,
        step={
            "id": "winds",
            "kind": "winds",
            "title": "Multi-Doppler winds from " + ", ".join(radars),
            "why": "Two or more radars with velocity and good beam-crossing geometry: the 3-D wind can be retrieved where their beams cross at a wide angle.",
            "needs": "radarx; de-aliased velocity (automatic); volumes within a few minutes of each other",
            "produces": "winds_<time>.nc (u, v, w, speed) + .png",
            "provenance": "RETRIEVED (analytic dual-Doppler with mass continuity; w is weakest, see docs/MULTIDOPPLER.md limits)",
            "inputs": [p for n in radars for p in _files_of(inv, n)],
            "radars": radars,
            "rxd": "rxd winds -i {inputs} -o winds.nc -p winds.png",
            "weight": 4.0,
        },
    )


@rule("qpe", "Rainfall accumulation", rank=50)
def _r_qpe(inv: Inventory) -> Outcome:
    ev, ok = [], []
    for r in _radars(inv).values():
        if not r["has_reflectivity"] or "ppi" not in r["kinds"]:
            ev.append(f"{r['name']}: no PPI reflectivity")
            continue
        if r["n_files"] < 2:
            ev.append(f"{r['name']}: one volume: a rain RATE is possible, an accumulation needs a series")
            continue
        ok.append(r["name"])
        ev.append(f"{r['name']}: {r['n_files']} volumes over {r['span_s'] / 60:.0f} min" + (", dual-pol: hybrid Z/KDP estimator possible" if r["dual_pol"] else ", Z-R only"))
    if not ok:
        return Outcome(False, ev, refusal={"what": "rainfall accumulation", "missing": "a time series (>= 2 volumes) of PPI reflectivity"})
    return Outcome(
        True,
        ev,
        step={
            "id": "qpe",
            "kind": "qpe",
            "title": "Rainfall accumulation with uncertainty",
            "why": "A series of reflectivity volumes: rain rate per volume, accumulated over the period, with the Z-R scatter as uncertainty.",
            "needs": "radarx; band for the Z-R relation" + ("; dual-pol fields for the hybrid method" if any(_radars(inv)[n]["dual_pol"] for n in ok) else ""),
            "produces": "qpe_accumulation.nc + .png (mm), uncertainty field",
            "provenance": "RETRIEVED (Z-R / hybrid estimator; uncertainty from the relation's scatter, not from gauges)",
            "inputs": [p for n in ok for p in _files_of(inv, n)],
            "radars": ok,
            "rxd": "rxd qpe -i {inputs} -o qpe.nc -p qpe.png",
            "weight": 1.2,
        },
    )


@rule("movie", "Animation of the time series", rank=60)
def _r_movie(inv: Inventory) -> Outcome:
    ev, ok = [], []
    for r in _radars(inv).values():
        if r["n_files"] >= 3 and "ppi" in r["kinds"]:
            ok.append(r["name"])
            ev.append(f"{r['name']}: {r['n_files']} volumes, {r['span_s'] / 60:.0f} min")
    if not ok:
        return Outcome(False, ev or ["fewer than 3 volumes per radar"], refusal={"what": "animation", "missing": "at least 3 volumes of one radar"})
    return Outcome(
        True,
        ev,
        step={
            "id": "movie",
            "kind": "movie",
            "title": "Storm animation (3D clip)",
            "why": "A series: a storm-centred clip shows the evolution; frames are interpolated between scans and labelled as such.",
            "needs": "system Chrome and PyAV (rendered by 'rxd animate', not inside this run)",
            "produces": "animate_<radars>_<time>.mp4",
            "provenance": "OBSERVED data; the in-between frames are SIMULATED/STYLISED (interpolated, labelled)",
            "inputs": [p for n in ok for p in _files_of(inv, n)],
            "radars": ok,
            "rxd": "rxd animate -i {inputs}",
            "manual": True,
            "weight": 3.0,
        },
    )


@rule("rhi", "RHI cross-sections", rank=70)
def _r_rhi(inv: Inventory) -> Outcome:
    ev, ok = [], []
    for r in _radars(inv).values():
        if r["rhi"]:
            ok.append(r["name"])
            ev.append(f"{r['name']}: RHI sweeps present")
    if not ok:
        return Outcome(False, ["no RHI scans"])
    return Outcome(
        True,
        ev,
        step={
            "id": "rhi",
            "kind": "plot",
            "title": "RHI range-height plots",
            "why": "RHI scans are vertical slices already: plot them as range-height figures.",
            "needs": "nothing else",
            "produces": "rhi_<file>.png per scan",
            "provenance": "OBSERVED",
            "inputs": [p for n in ok for p in _files_of(inv, n)],
            "rxd": "rxd plot -i {inputs} -o rhi.png",
            "weight": 0.4,
        },
    )


@rule("problems", "Problems to be aware of", rank=90)
def _r_problems(inv: Inventory) -> Outcome:
    ev: list[str] = []
    for name, rep in inv.get("checks", {}).items():
        for f in rep.get("findings", []):
            if f["severity"] == "warning":
                ev.append(f"{name}: {f['title']}: {f['summary']}")
    for r in _radars(inv).values():
        for g in r["gaps"][:5]:
            ev.append(f"{r['name']}: gap of {g['seconds'] / 60:.0f} min after {g['after']}")
    for u in inv.get("unreadable", [])[:5]:
        ev.append(f"{u['name']}: {u['reason']}")
    if inv["radars"].get("UNKNOWN"):
        ev.append(f"{inv['radars']['UNKNOWN']['n_files']} file(s) of unknown radar/time (not opened; run again with fewer files or name them)")
    return Outcome(bool(ev), ev or ["nothing flagged"], note="; ".join(ev) if ev else None)


def evaluate_rules(inv: Inventory) -> list[dict[str, Any]]:
    """Run every rule; the table of ``{id, title, rank, fired, evidence, step?, refusal?, note?}``."""
    table: list[dict[str, Any]] = []
    for r in sorted(RULES, key=lambda x: x.rank):
        try:
            out = r.fn(inv)
        except Exception as exc:  # a broken rule must not hide the others
            out = Outcome(False, [f"rule failed: {type(exc).__name__}: {exc}"])
        row: dict[str, Any] = {"id": r.id, "title": r.title, "rank": r.rank, "fired": out.fired, "evidence": out.evidence}
        if out.step:
            row["step"] = {**out.step, "rule": r.id}
        if out.refusal:
            row["refusal"] = out.refusal
        if out.note:
            row["note"] = out.note
        table.append(row)
    return table

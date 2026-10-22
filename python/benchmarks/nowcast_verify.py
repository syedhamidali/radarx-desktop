"""The nowcast verification run behind docs/NOWCAST.md (not part of the installed package).

Steps, each reproducible from the run record it writes:

1. frames: every event folder of ``nowcast_events.py`` (downloaded on purpose with
   ``--download``) is gridded by the mosaic engine into composite-reflectivity frames on a fixed
   1 km grid (cached as ``<event>_frames.nc`` next to the volumes; delete to regrid);
2. tuning: the motion-error statistics of the method are measured on the **tuning** events only
   and written to ``engine/nowcast/motion_error_default.json`` (what the ensemble perturbs with);
3. verification: the **verification** events are scored against Eulerian persistence at every
   lead; the pooled scores, the paired block-bootstrap intervals over events and the per-event
   tables are written to ``--out`` (JSON + CSV + figures for the docs).

    python benchmarks/nowcast_verify.py --dir ~/nowcast_cases --out ../docs/images/nowcast
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import numpy as np
from nowcast_events import EVENTS, is_volume
from watchdog import peak_rss_gb, start_watchdog

from engine import provenance as prov
from engine.api_results import jsonable
from engine.nowcast import ensemble as ens
from engine.nowcast import frames as nf
from engine.nowcast import harness
from engine.nowcast import verify as vf

LEAD_MIN = 90.0
STEP_MIN = 5.0
N_MEMBERS = 10
RESOLUTION_M = 1000.0
MAX_RANGE_KM = 200.0
THRESHOLDS = (20.0, 35.0, 45.0)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def raw_volumes(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if is_volume(p))


def frames_of(
    ev: dict, root: Path, workers: int = 1, *, delete_volumes: bool = False
) -> nf.FrameSequence | None:
    """The event's frames: from the cache, or gridded from the volumes (and cached).

    With ``delete_volumes`` the Level II files are removed once the cache is written: the
    frames (a few MB) are all the harness needs, the volumes (hundreds of MB) can be fetched
    again with ``nowcast_events.py --download``.
    """
    folder = root / f"{ev['id']}_{ev['radar']}"
    cache = folder / f"{ev['id']}_frames.nc"
    if not cache.exists():
        files = raw_volumes(folder) if folder.is_dir() else []
        if not files:
            return None
        t0 = time.perf_counter()
        seq = nf.from_radar_files(
            files,
            resolution_m=RESOLUTION_M,
            max_range_km=MAX_RANGE_KM,
            workers=workers,
            on_error="skip",  # a volume xradar cannot read is left out and noted
            progress=lambda p, m: log(f"    [{p:3d}%] {m}") if p % 20 == 0 else None,
        )
        log(f"  {ev['id']}: {seq.n} frames gridded in {time.perf_counter() - t0:.0f} s")
        for note in seq.notes:
            log(f"    note: {note}")
        seq.to_netcdf(cache)
    if delete_volumes and cache.exists():
        files = raw_volumes(folder)
        freed = sum(p.stat().st_size for p in files)
        for p in files:
            p.unlink()
        if files:
            log(f"  {ev['id']}: {len(files)} volumes deleted ({freed / 1e6:.0f} MB)")
    return nf.from_netcdf([cache])


def write_manifest(root: Path) -> dict[str, Any]:
    """``manifest.json`` in the data folder: what is on disk per event and in total."""
    events = {}
    total = 0
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        files = list(folder.iterdir())
        raw = raw_volumes(folder)
        caches = [p for p in files if p.suffix == ".nc"]
        size = sum(p.stat().st_size for p in files if p.is_file())
        total += size
        events[folder.name] = {
            "volumes": len(raw),
            "volumes_mb": round(sum(p.stat().st_size for p in raw) / 1e6, 1),
            "frames_mb": round(sum(p.stat().st_size for p in caches) / 1e6, 1),
            "mb": round(size / 1e6, 1),
        }
    manifest = {"updated": prov.utc_stamp(), "total_mb": round(total / 1e6, 1), "events": events}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def event_facts(seq: nf.FrameSequence) -> dict[str, Any]:
    ef = seq.echo_fraction(20.0)
    ef35 = seq.echo_fraction(35.0)
    return {
        "n_frames": seq.n,
        "step_min": round(seq.step_s() / 60.0, 2),
        "first": prov.iso_utc(seq.times[0]),
        "last": prov.iso_utc(seq.times[-1]),
        "echo_fraction_ge_20dbz_mean": round(float(ef.mean()), 4),
        "echo_fraction_ge_35dbz_mean": round(float(ef35.mean()), 4),
        "echo_fraction_ge_20dbz_max": round(float(ef.max()), 4),
        "notes": list(seq.notes),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dir", default="~/nowcast_cases")
    ap.add_argument("--out", default=str(HERE.parent.parent / "docs" / "images" / "nowcast"))
    ap.add_argument("--only", default="")
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--origin-stride", type=int, default=2, help="every n-th origin (2: 10 min)")
    ap.add_argument("--workers", type=int, default=3, help="processes that grid the volumes")
    ap.add_argument("--max-rss-gb", type=float, default=6.0, help="abort above this memory")
    ap.add_argument("--members", type=int, default=N_MEMBERS)
    ap.add_argument("--skip-tuning", action="store_true", help="use the shipped statistics")
    ap.add_argument("--frames-only", action="store_true", help="grid (and cache) the frames, stop")
    ap.add_argument(
        "--replot", action="store_true", help="figures and tables from the saved record only"
    )
    ap.add_argument(
        "--delete-volumes",
        action="store_true",
        help="remove the Level II files once an event's frames are cached (disk)",
    )
    args = ap.parse_args()
    start_watchdog(max_rss_gb=args.max_rss_gb, min_free_disk_gb=20.0, max_swap_gb=8.0, log=log)
    root = Path(args.dir).expanduser()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    if args.replot:
        return replot(out)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    events = [e for e in EVENTS if not only or e["id"] in only]
    t_start = time.perf_counter()
    record: dict[str, Any] = {
        "created": prov.utc_stamp(),
        "software": prov.software_string(),
        "libraries": prov.library_versions(),
        "settings": {
            "lead_min": LEAD_MIN,
            "step_min": STEP_MIN,
            "n_members": args.members,
            "resolution_m": RESOLUTION_M,
            "max_range_km": MAX_RANGE_KM,
            "thresholds_dbz": THRESHOLDS,
            "origin_stride": args.origin_stride,
            "n_boot": args.n_boot,
            "seed": 0,
        },
        "events": {},
    }

    # 1 + 2: frames and tuning --------------------------------------------------------------
    seqs: dict[str, nf.FrameSequence] = {}
    for ev in events:
        log(f"frames {ev['id']} {ev['radar']}")
        try:
            seq = frames_of(ev, root, args.workers, delete_volumes=args.delete_volumes)
        except Exception as exc:
            log(f"  {ev['id']}: failed: {exc}")
            record["events"][ev["id"]] = {**ev, "error": str(exc)}
            continue
        if seq is None:
            record["events"][ev["id"]] = {**ev, "error": "no files"}
            continue
        seqs[ev["id"]] = seq
        record["events"][ev["id"]] = {**ev, **event_facts(seq)}
    manifest = write_manifest(root)
    log(f"data folder {root}: {manifest['total_mb']:.0f} MB (manifest.json)")
    if args.frames_only:
        return 0
    stats_path = HERE.parent / "engine" / "nowcast" / "motion_error_default.json"
    if not args.skip_tuning:
        samples: dict[str, list[dict[str, Any]]] = {}
        leads = None
        for ev in events:
            if ev["role"] != "tuning" or ev["id"] not in seqs:
                continue
            log(f"tuning {ev['id']}")
            res = harness.motion_error_samples(
                seqs[ev["id"]], lead_min=LEAD_MIN, step_min=STEP_MIN, origin_stride=1
            )
            samples[ev["id"]] = res["samples"]
            leads = res["leads_min"]
        if samples and leads:
            stats = harness.fit_motion_error(
                samples,
                leads,
                source=(
                    "verified on the tuning events "
                    + ", ".join(
                        f"{k} ({record['events'][k]['radar']} {record['events'][k]['start']})"
                        for k in samples
                    )
                    + ": std of (motion used at t) - (mean pair motion over t..t+lead), "
                    "echo-weighted domain means"
                ),
            )
            stats_path.write_text(json.dumps(stats.to_dict(), indent=2) + "\n")
            record["motion_error_statistics"] = stats.to_dict()
            write_csv(
                out / "motion_error_samples.csv",
                [{"event": e, **s} for e, ss in samples.items() for s in ss],
            )
            log(
                f"  wrote {stats_path.name}: parallel {stats.parallel[:3]}..., "
                f"perpendicular {stats.perpendicular[:3]}..."
            )
    stats = ens.MotionErrorStats.load(stats_path)

    # 3: verification -------------------------------------------------------------------------
    records: list[vf.Record] = []
    per_event: dict[str, Any] = {}
    for ev in events:
        if ev["role"] != "verification" or ev["id"] not in seqs:
            continue
        log(f"verify {ev['id']}")
        t0 = time.perf_counter()
        recs = harness.verify_sequence(
            seqs[ev["id"]],
            event=ev["id"],
            lead_min=LEAD_MIN,
            step_min=STEP_MIN,
            n_members=args.members,
            thresholds=THRESHOLDS,
            motion_stats=stats,
            origin_stride=args.origin_stride,
            progress=lambda p, m: log(f"    [{p:3d}%] {m}") if p % 25 == 0 else None,
        )
        records += recs
        per_event[ev["id"]] = harness.summarise(recs, n_boot=0)
        log(f"  {ev['id']}: {len(recs)} records in {time.perf_counter() - t0:.0f} s")
    if not records:
        log("nothing to verify")
        return 1
    summary = harness.summarise(records, n_boot=args.n_boot)
    record["summary"] = summary
    record["per_event"] = per_event
    record["elapsed_s"] = round(time.perf_counter() - t_start, 1)
    record["peak_rss_gb"] = round(peak_rss_gb(), 2)
    with gzip.open(out / RECORD_NAME, "wt", encoding="utf-8") as fh:  # ~6 MB as text
        fh.write(json.dumps(jsonable(record), indent=1) + "\n")
    write_tables_and_figures(summary, per_event, record, out)
    log(f"done in {record['elapsed_s']} s -> {out}")
    return 0


RECORD_NAME = "verification_record.json.gz"


def _numeric_keys(obj: Any) -> Any:
    """JSON turned the lead, threshold and scale keys into strings; back to numbers."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            try:
                kk: Any = float(k)
            except (TypeError, ValueError):
                kk = k
            out[kk] = _numeric_keys(v)
        return out
    if isinstance(obj, list):
        return [_numeric_keys(v) for v in obj]
    return obj


def replot(out: Path) -> int:
    """Figures and tables again from the saved record (no scoring)."""
    with gzip.open(out / RECORD_NAME, "rt", encoding="utf-8") as fh:
        record = json.load(fh)
    summary = _numeric_keys(record["summary"])
    per_event = {k: _numeric_keys(v) for k, v in record["per_event"].items()}
    write_tables_and_figures(summary, per_event, record, out)
    log(f"figures and tables rebuilt from {out / RECORD_NAME}")
    return 0


def write_tables_and_figures(
    summary: dict[str, Any], per_event: dict[str, Any], record: dict[str, Any], out: Path
) -> None:
    thresholds = summary["thresholds"]

    # flat tables --------------------------------------------------------------------------
    rows = []
    for m, by_lead in summary["by_model_lead"].items():
        for ld, s in by_lead.items():
            for t, c in s["categorical"].items():
                rows.append(
                    {
                        "model": m,
                        "lead_min": ld,
                        "threshold_dbz": t,
                        **{k: c[k] for k in ("pod", "far", "csi", "bias", "ets", "base_rate")},
                        "fss_" + "_".join(str(k) for k in s["fss"].get(t, {})): None,
                        **{f"fss_{sc}": v for sc, v in s["fss"].get(t, {}).items()},
                        "brier": s["prob"].get(t, {}).get("brier"),
                        "bss_clim": s["prob"].get(t, {}).get("bss_climatology"),
                        "roc_area": s["prob"].get(t, {}).get("roc_area"),
                        "mae_mm_h": s["continuous"]["mae"],
                        "rmse_mm_h": s["continuous"]["rmse"],
                        "corr": s["continuous"]["corr"],
                        "crps": s.get("crps"),
                        "spread": s.get("spread_mean"),
                        "n_forecasts": s["n_forecasts"],
                    }
                )
    write_csv(out / "scores_pooled.csv", rows)
    rows = []
    for m, by_lead in summary["skill_vs_reference"].items():
        for ld, row in by_lead.items():
            for k, v in row.items():
                rows.append(
                    {
                        "model": m,
                        "lead_min": ld,
                        "statistic": k,
                        "difference": v["value"],
                        "ci_low": v["ci"][0],
                        "ci_high": v["ci"][1],
                        "n_events": v["n_events"],
                    }
                )
    write_csv(out / "skill_vs_persistence.csv", rows)
    rows = []
    for e, s in per_event.items():
        for m, by_lead in s["by_model_lead"].items():
            for ld, sc in by_lead.items():
                c = sc["categorical"].get(thresholds[0], {})
                rows.append(
                    {
                        "event": e,
                        "mode": record["events"][e]["mode"],
                        "model": m,
                        "lead_min": ld,
                        "csi_20": c.get("csi"),
                        "pod_20": c.get("pod"),
                        "far_20": c.get("far"),
                        "csi_35": sc["categorical"].get(thresholds[1], {}).get("csi"),
                        "fss_20_9km": sc["fss"].get(thresholds[0], {}).get(9),
                        "mae_mm_h": sc["continuous"]["mae"],
                        "brier_20": sc["prob"].get(thresholds[0], {}).get("brier"),
                        "crps": sc.get("crps"),
                    }
                )
    write_csv(out / "scores_per_event.csv", rows)
    make_figures(summary, per_event, record, out)
    (out / "verification_tables.txt").write_text(markdown_tables(summary, per_event, record))


# ---------------------------------------------------------------------------
# Figures (colour-blind safe: Okabe-Ito)
# ---------------------------------------------------------------------------

COLOURS = {"lucas_kanade": "#0072B2", "global": "#E69F00", "eulerian": "#555555"}
LABELS = {
    "lucas_kanade": "Nowcast (dense optical flow)",
    "global": "Nowcast (one translation)",
    "eulerian": "Eulerian persistence",
}


def _f(v: Any, spec: str = ".2f") -> str:
    return "-" if v is None else format(v, spec)


def _ci(r: dict[str, Any] | None, spec: str = "+.2f") -> str:
    if not r or r.get("value") is None:
        return "-"
    lo, hi = r["ci"]
    if lo is None:
        return format(r["value"], spec)
    return f"{r['value']:{spec}} [{lo:{spec}}, {hi:{spec}}]"


def markdown_tables(
    summary: dict[str, Any], per_event: dict[str, Any], record: dict[str, Any]
) -> str:
    """The tables of docs/NOWCAST.md, generated from the run (pasted there by hand)."""
    thr = summary["thresholds"]
    leads = summary["leads_min"]
    show = [ld for ld in leads if ld in (5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0, 90.0)]
    lines = ["<!-- generated by the verification run; do not edit by hand -->", ""]
    bm = summary["by_model_lead"]
    diff = summary["skill_vs_reference"].get("lucas_kanade", {})

    def row(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    def csi(scores: dict[str, Any], t: float) -> Any:
        return scores.get("categorical", {}).get(t, {}).get("csi")

    def fss9(scores: dict[str, Any], t: float) -> Any:
        return scores.get("fss", {}).get(t, {}).get(9)

    # events
    lines += [
        row(
            [
                "id",
                "radar",
                "window (UTC)",
                "season; storm mode",
                "frames",
                "cadence",
                "echo >= 20 dBZ (mean fraction of the covered area)",
            ]
        ),
        "|---|---|---|---|---|---|---|",
    ]
    for eid, ev in record["events"].items():
        if ev.get("role") != "verification" or "error" in ev:
            continue
        lines.append(
            row(
                [
                    eid,
                    ev["radar"],
                    f"{ev['first'][:16].replace('T', ' ')} to {ev['last'][11:16]}",
                    f"{ev['season']}; {ev['mode']}",
                    str(ev["n_frames"]),
                    f"{ev['step_min']:.1f} min",
                    f"{100 * ev['echo_fraction_ge_20dbz_mean']:.1f} %",
                ]
            )
        )
    lines.append("")
    # pooled scores per lead
    for t in thr:
        lines += [
            f"**Reflectivity >= {t:g} dBZ, pooled over the verification events** (CSI; FSS at "
            "9 km; the nowcast's CSI minus persistence's with its 95 % block-bootstrap interval "
            "over events):",
            "",
            row(
                [
                    "lead",
                    "CSI nowcast",
                    "CSI one-vector",
                    "CSI persistence",
                    "FSS 9 km nowcast",
                    "FSS 9 km persistence",
                    "CSI nowcast - persistence [95 % interval]",
                ]
            ),
            "|---|---|---|---|---|---|---|",
        ]
        for ld in show:
            lk = bm.get("lucas_kanade", {}).get(ld, {})
            gl = bm.get("global", {}).get(ld, {})
            eu = bm.get("eulerian", {}).get(ld, {})
            lines.append(
                row(
                    [
                        f"+{ld:g} min",
                        _f(csi(lk, t)),
                        _f(csi(gl, t)),
                        _f(csi(eu, t)),
                        _f(fss9(lk, t)),
                        _f(fss9(eu, t)),
                        _ci(diff.get(ld, {}).get(f"csi_{t:g}")),
                    ]
                )
            )
        lines.append("")
    # rain rate and probabilistic
    lines += [
        "**Rain rate (mm/h, WSR-88D Z-R) and the ensemble**, pooled:",
        "",
        row(
            [
                "lead",
                "MAE nowcast",
                "MAE persistence",
                "MAE difference [95 % interval]",
                "CRPS ensemble",
                "spread",
                "Brier >= 20 dBZ nowcast",
                "Brier persistence (0/1)",
                "BSS vs climatology",
                "ROC area",
            ]
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for ld in show:
        lk = bm.get("lucas_kanade", {}).get(ld, {})
        eu = bm.get("eulerian", {}).get(ld, {})
        p = lk.get("prob", {}).get(thr[0], {})
        pe = eu.get("prob", {}).get(thr[0], {})
        lines.append(
            row(
                [
                    f"+{ld:g} min",
                    _f(lk.get("continuous", {}).get("mae"), ".3f"),
                    _f(eu.get("continuous", {}).get("mae"), ".3f"),
                    _ci(diff.get(ld, {}).get("mae"), "+.3f"),
                    _f(lk.get("crps"), ".3f"),
                    _f(lk.get("spread_mean"), ".3f"),
                    _f(p.get("brier"), ".4f"),
                    _f(pe.get("brier"), ".4f"),
                    _f(p.get("bss_climatology")),
                    _f(p.get("roc_area")),
                ]
            )
        )
    lines.append("")
    # per event at 15 .. 90 min
    lines += [
        "**By event** (CSI >= 20 dBZ, nowcast / persistence; the lead at which the nowcast stops "
        "beating persistence by at least 0.02 CSI, `>90` when it still does at the longest lead):",
        "",
        row(["event", "storm mode", "+15", "+30", "+60", "+90 min", "skill lost after"]),
        "|---|---|---|---|---|---|---|",
    ]
    for eid, s in per_event.items():
        lost = ">90"
        for ld in leads:
            lk = csi(s["by_model_lead"].get("lucas_kanade", {}).get(ld, {}), thr[0])
            eu = csi(s["by_model_lead"].get("eulerian", {}).get(ld, {}), thr[0])
            if lost == ">90" and lk is not None and eu is not None and lk - eu < 0.02:
                lost = f"{ld:g} min"
        cells = []
        for ld in (15.0, 30.0, 60.0, 90.0):
            lk = csi(s["by_model_lead"].get("lucas_kanade", {}).get(ld, {}), thr[0])
            eu = csi(s["by_model_lead"].get("eulerian", {}).get(ld, {}), thr[0])
            cells.append(f"{_f(lk)} / {_f(eu)}")
        lines.append(row([eid, record["events"][eid]["mode"], *cells, lost]))
    lines.append("")
    st = record.get("motion_error_statistics")
    if st:
        keep = [i for i, v in enumerate(st["leads_min"]) if v in show]
        lines += [
            "**Motion error used by the ensemble** (standard deviation in m/s of the motion used "
            "at the origin minus the motion realised over the lead, tuning events only):",
            "",
            row(["lead", *(f"+{st['leads_min'][i]:g}" for i in keep)]),
            "|---|" + "---|" * len(keep),
            row(["along the motion", *(f"{st['parallel_ms'][i]:.2f}" for i in keep)]),
            row(["across the motion", *(f"{st['perpendicular_ms'][i]:.2f}" for i in keep)]),
            "",
        ]
    return "\n".join(lines) + "\n"


def make_figures(
    summary: dict[str, Any], per_event: dict[str, Any], record: dict[str, Any], out: Path
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {"font.size": 8, "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 150}
    )
    leads = summary["leads_min"]
    thr = summary["thresholds"]
    models = [m for m in ("lucas_kanade", "global", "eulerian") if m in summary["models"]]

    # 1. skill versus lead: CSI at each threshold, FSS at 9 km, MAE
    fig, axes = plt.subplots(1, 4, figsize=(11, 2.9), constrained_layout=True)
    for ax, t in zip(axes[:3], thr, strict=False):
        for m in models:
            ys = [
                summary["by_model_lead"][m].get(ld, {}).get("categorical", {}).get(t, {}).get("csi")
                for ld in leads
            ]
            ax.plot(
                leads,
                [np.nan if v is None else v for v in ys],
                "-o",
                ms=3,
                color=COLOURS[m],
                label=LABELS[m],
            )
        ax.set_title(f"CSI, reflectivity >= {t:g} dBZ")
        ax.set_xlabel("lead time [min]")
        ax.set_ylabel("CSI [1]")
        ax.set_ylim(0, 1)
    ax = axes[3]
    for m in models:
        ys = [
            summary["by_model_lead"][m].get(ld, {}).get("continuous", {}).get("mae") for ld in leads
        ]
        ax.plot(
            leads,
            [np.nan if v is None else v for v in ys],
            "-o",
            ms=3,
            color=COLOURS[m],
            label=LABELS[m],
        )
    ax.set_title("MAE of the rain rate")
    ax.set_xlabel("lead time [min]")
    ax.set_ylabel("MAE [mm/h]")
    ax.set_ylim(bottom=0)
    axes[0].legend(fontsize=7, frameon=False)
    fig.suptitle(
        f"Nowcast skill versus lead time, pooled over {len(summary['events'])} verification "
        "events  (FORECAST verification; scores of the extrapolation, not an official forecast)",
        fontsize=8,
        x=0.01,
        ha="left",
    )
    fig.savefig(out / "skill_vs_lead.png")
    plt.close(fig)

    # 2. skill relative to persistence with bootstrap intervals (CSI 20 dBZ and MAE)
    fig, axes = plt.subplots(1, 2, figsize=(8, 3), constrained_layout=True)
    for ax, key, label in (
        (axes[0], f"csi_{thr[0]:g}", f"CSI(>= {thr[0]:g} dBZ): nowcast - persistence"),
        (axes[1], "mae", "MAE(rain rate): nowcast - persistence [mm/h]"),
    ):
        for m in models:
            if m == "eulerian":
                continue
            rows = summary["skill_vs_reference"].get(m, {})
            xs, ys, lo, hi = [], [], [], []
            for ld in leads:
                r = rows.get(ld, {}).get(key)
                if not r or r["value"] is None:
                    continue
                xs.append(ld)
                ys.append(r["value"])
                lo.append(r["ci"][0] if r["ci"][0] is not None else r["value"])
                hi.append(r["ci"][1] if r["ci"][1] is not None else r["value"])
            if xs:
                ax.fill_between(xs, lo, hi, color=COLOURS[m], alpha=0.18, lw=0)
                ax.plot(xs, ys, "-o", ms=3, color=COLOURS[m], label=LABELS[m])
        ax.axhline(0, color="k", lw=0.8)
        ax.set_title(label)
        ax.set_xlabel("lead time [min]")
        ax.set_ylabel("difference")
    axes[0].legend(fontsize=7, frameon=False)
    fig.suptitle(
        "Skill relative to Eulerian persistence (95 % paired block bootstrap over events; "
        "CSI above zero and MAE below zero: the nowcast is better)",
        fontsize=7.5,
        x=0.01,
        ha="left",
    )
    fig.savefig(out / "skill_vs_persistence.png")
    plt.close(fig)

    # 3. per-event CSI at (about) 30 min: storm mode effect. An event whose scan cadence has no
    # observation within half a step of 30 min is shown at its nearest scored lead (labelled).
    ev_ids = list(per_event)
    ld30 = min(leads, key=lambda v: abs(v - 30.0))
    fig, ax = plt.subplots(figsize=(8.5, 3.4), constrained_layout=True)
    width = 0.27
    ev_lead = {}
    for e in ev_ids:
        scored = [
            ld
            for ld in leads
            if per_event[e]["by_model_lead"].get("eulerian", {}).get(ld, {}).get("categorical")
        ]
        ev_lead[e] = min(scored, key=lambda v: abs(v - 30.0)) if scored else ld30
    for i, m in enumerate(models):
        vals = [
            per_event[e]["by_model_lead"]
            .get(m, {})
            .get(ev_lead[e], {})
            .get("categorical", {})
            .get(thr[0], {})
            .get("csi")
            for e in ev_ids
        ]
        ax.bar(
            np.arange(len(ev_ids)) + (i - 1) * width,
            [np.nan if v is None else v for v in vals],
            width,
            color=COLOURS[m],
            label=LABELS[m],
        )
    ax.set_xticks(np.arange(len(ev_ids)))
    ax.set_xticklabels(
        [
            f"{e} {record['events'][e]['radar']}, +{ev_lead[e]:g} min\n"
            + _wrap(record["events"][e]["mode"], 16)
            for e in ev_ids
        ],
        fontsize=6,
    )
    ax.set_ylabel(f"CSI (>= {thr[0]:g} dBZ) [1]")
    ax.set_ylim(0, 1)
    ax.legend(fontsize=7, frameon=False)
    ax.set_title("Skill by event (storm mode) at the scored lead nearest 30 min", loc="left")
    fig.savefig(out / "skill_by_event.png")
    plt.close(fig)

    # 4. reliability diagram and ROC at 30 min for the lowest threshold, plus spread/skill
    fig, axes = plt.subplots(1, 3, figsize=(10, 3.2), constrained_layout=True)
    s = summary["by_model_lead"].get("lucas_kanade", {}).get(ld30, {}).get("prob", {}).get(thr[0])
    ax = axes[0]
    ax.plot([0, 1], [0, 1], "k--", lw=0.8, label="perfect reliability")
    if s:
        pts = [
            (r["forecast_mean"], r["observed_frequency"], r["n"])
            for r in s["reliability"]
            if r["n"] > 0
        ]
        ax.plot(
            [p[0] for p in pts],
            [p[1] for p in pts],
            "-o",
            ms=3,
            color=COLOURS["lucas_kanade"],
            label=f"ensemble, +{ld30:g} min",
        )
        for p in pts:
            ax.annotate(
                f"{int(p[2])}",
                (p[0], p[1]),
                fontsize=5.5,
                textcoords="offset points",
                xytext=(3, 3),
            )
        ax.axhline(
            s["base_rate"], color="grey", lw=0.6, ls=":", label=f"base rate {s['base_rate']:.3f}"
        )
    ax.set_xlabel(f"forecast probability of >= {thr[0]:g} dBZ [1]")
    ax.set_ylabel("observed frequency [1]")
    ax.set_title("Reliability (counts per bin)", loc="left")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend(fontsize=6.5, frameon=False)
    ax = axes[1]
    ax.plot([0, 1], [0, 1], "k--", lw=0.8)
    for ldx, ls in (
        (min(leads, key=lambda v: abs(v - 15.0)), "-"),
        (ld30, "--"),
        (min(leads, key=lambda v: abs(v - 60.0)), ":"),
    ):
        t = _roc_points(summary, ldx, thr[0])
        if t:
            ax.plot(
                t[0],
                t[1],
                ls,
                color=COLOURS["lucas_kanade"],
                label=f"+{ldx:g} min, area {t[2]:.2f}",
            )
    ax.set_xlabel("false alarm rate [1]")
    ax.set_ylabel("hit rate [1]")
    ax.set_title(f"ROC, >= {thr[0]:g} dBZ", loc="left")
    ax.legend(fontsize=6.5, frameon=False)
    ax = axes[2]
    xs, crps, mae, spread = [], [], [], []
    for ld in leads:
        s2 = summary["by_model_lead"].get("lucas_kanade", {}).get(ld, {})
        if s2.get("crps") is None:
            continue
        xs.append(ld)
        crps.append(s2["crps"])
        mae.append(s2.get("crps_mae_deterministic"))
        spread.append(s2.get("spread_mean"))
    if xs:
        ax.plot(xs, crps, "-o", ms=3, color=COLOURS["lucas_kanade"], label="CRPS of the ensemble")
        ax.plot(xs, mae, "-s", ms=3, color=COLOURS["global"], label="MAE of member 0 (= its CRPS)")
        ax.plot(xs, spread, "-^", ms=3, color="#009E73", label="ensemble spread (std)")
    ax.set_xlabel("lead time [min]")
    ax.set_ylabel("mm/h")
    ax.set_ylim(bottom=0)
    ax.set_title("CRPS, MAE and spread (rain rate)", loc="left")
    ax.legend(fontsize=6.5, frameon=False)
    fig.suptitle(
        "Probabilistic verification of the motion-perturbation ensemble "
        "(pooled over the verification events)",
        fontsize=8,
        x=0.01,
        ha="left",
    )
    fig.savefig(out / "probabilistic.png")
    plt.close(fig)


def _wrap(text: str, width: int, max_lines: int = 3) -> str:
    import textwrap

    lines = textwrap.wrap(text, width)
    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], lines[max_lines - 1][: width - 1] + "…"]
    return "\n".join(lines)


def _roc_points(
    summary: dict[str, Any], lead: float, thr: float
) -> tuple[list[float], list[float], float] | None:
    s = summary["by_model_lead"].get("lucas_kanade", {}).get(lead, {}).get("prob", {}).get(thr)
    if not s or s.get("roc_area") is None:
        return None
    # rebuild the curve from the reliability counts is not possible; recompute from the record
    # is not needed: the ROC counts are pooled in the summary via probabilistic_scores only as
    # area. Draw the area as the label and the diagonal; points come from the per-bin counts.
    pts_x, pts_y = [0.0], [0.0]
    rel = s["reliability"]
    events = sum(r["n"] * (r["observed_frequency"] or 0.0) for r in rel if r["n"] > 0)
    non = sum(r["n"] * (1.0 - (r["observed_frequency"] or 0.0)) for r in rel if r["n"] > 0)
    h = f = 0.0
    for r in reversed(rel):
        if r["n"] <= 0:
            continue
        h += r["n"] * (r["observed_frequency"] or 0.0)
        f += r["n"] * (1.0 - (r["observed_frequency"] or 0.0))
        pts_x.append(f / non if non > 0 else 0.0)
        pts_y.append(h / events if events > 0 else 0.0)
    pts_x.append(1.0)
    pts_y.append(1.0)
    return pts_x, pts_y, float(s["roc_area"])


if __name__ == "__main__":
    sys.exit(main())

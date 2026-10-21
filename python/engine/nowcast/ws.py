"""WebSocket messages of the nowcast (the server only forwards them here).

Protocol (JSON; a request carries ``request_id`` that the reply echoes)::

    -> {type: "nowcast_run", request_id, file_ids: [...open files in time order...],
        options: {lead_min, step_min, n_members, motion_method, n_frames, origin,
                  resolution_m, max_range_km, extent, variable, product, group_s,
                  zr_relation, decay, seed, thresholds, rate_thresholds, layers}}
    <- progress ..., {type: "nowcast_result", request_id, nowcast_id, origin_time, leads_min,
        valid_times, field, units, grid, corners, motion, stats, notes, provenance, record,
        layers: [{lead_min, kind: "deterministic"|"probability", threshold?, png_b64, vmin,
        vmax, legend, label}]}
    <- {type: "nowcast_error", request_id, message}
    -> {type: "nowcast_cancel", request_id}                 (stops the run at its next check)
    -> {type: "nowcast_export", request_id, nowcast_id, path, overwrite?}   <- {type: "file_saved"}

``layers`` are map-ready PNGs on the same Web-Mercator image grid as a mosaic (with ``corners``),
so the existing map layer code can draw them; ``provenance`` is the ``forecast`` layer
provenance (method, lead, skill statement, the guidance sentence).
"""

from __future__ import annotations

import base64
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from engine import provenance as prov
from engine.nowcast import frames as nf
from engine.nowcast import run as nr

MAX_RESULTS = 2
MAX_LAYERS = 40


class NowcastStore:
    """The last results (their fields are tens of MB), newest kept."""

    def __init__(self, maxsize: int = MAX_RESULTS) -> None:
        self._items: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self.maxsize = maxsize

    def put(self, result: Any) -> str:
        nid = uuid.uuid4().hex[:12]
        with self._lock:
            self._items[nid] = result
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)
        return nid

    def get(self, nid: Any) -> Any:
        with self._lock:
            if not isinstance(nid, str) or nid not in self._items:
                raise nr.NowcastError("Unknown or expired nowcast: run it again")
            self._items.move_to_end(nid)
            return self._items[nid]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


STORE = NowcastStore()


def _num(options: dict[str, Any], key: str, default: float) -> float:
    v = options.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        raise nr.NowcastError(f"nowcast: '{key}' must be a number")
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise nr.NowcastError(f"nowcast: '{key}' must be a number") from exc


def _thresholds(options: dict[str, Any], key: str) -> list[float] | None:
    v = options.get(key)
    if v is None:
        return None
    if not isinstance(v, (list, tuple)) or not v:
        raise nr.NowcastError(f"nowcast: '{key}' must be a non-empty list of numbers")
    return [float(x) for x in v]


def build_frames(
    readers: list[tuple[str, Any]], options: dict[str, Any], progress: Any, cancel: Any
) -> nf.FrameSequence:
    """Frames from open readers (``(name, reader)``), gridded by the mosaic engine."""
    from engine import mosaic as me
    from engine.hydro.accumulate import scan_meta

    stamped = []
    for name, rd in readers:
        t = scan_meta(rd).time
        if np.isnat(t):
            raise nr.NowcastError(f"{name}: the volume has no scan time")
        stamped.append((t, (name, rd)))
    group_s = _num(options, "group_s", 150.0)
    groups = nf.group_by_time(stamped, group_s)
    extent = options.get("extent")
    if isinstance(extent, (list, tuple)) and len(extent) == 4:
        extent = {"west": extent[0], "south": extent[1], "east": extent[2], "north": extent[3]}
    frames, times, names, radars, notes = [], [], [], [], []
    layout: dict[str, Any] = {}
    covered = None
    for gi, grp in enumerate(groups):
        if cancel and cancel():
            raise nr.NowcastCancelled("cancelled")
        progress(5 + int(45 * gi / len(groups)), f"Gridding frame {gi + 1}/{len(groups)}")
        pairs = [(name, rd) for _, (name, rd) in grp]
        grid = {
            "resolution_m": _num(options, "resolution_m", 1000.0),
            "max_range_km": _num(options, "max_range_km", 230.0),
            "extent": layout.get("extent") or (extent if extent else "auto"),
        }
        rec = me.build_mosaic(
            pairs,
            str(options.get("variable") or "DBZH"),
            str(options.get("product") or "composite"),
            grid,
            {"enabled": True},
            cache=None,
            keep_volume=False,
        )
        merged = rec.merged_aeqd
        if merged is None:
            raise nr.NowcastError("the mosaic engine did not return the working grid")
        if not layout:
            w, s, e, n = rec.bbox
            layout = {
                "extent": {"west": w, "south": s, "east": e, "north": n},
                "origin": rec.origin,
                "x0": rec.x0,
                "y0": rec.y0,
                "cell_m": rec.cell_m,
                "shape": merged.shape,
                "bbox": rec.bbox,
            }
            covered = rec.covered_aeqd
        elif merged.shape != layout["shape"]:
            raise nr.NowcastError("the grid changed between frames")
        elif rec.covered_aeqd is not None and covered is not None:
            covered = covered | rec.covered_aeqd
        for r in rec.radars:
            if r.name not in {x["name"] for x in radars}:
                radars.append({"name": r.name, "latitude": r.latitude, "longitude": r.longitude})
        ts = [t for t, _ in grp]
        t0 = min(ts)
        tm = t0 + np.mean([(t - t0) / np.timedelta64(1, "s") for t in ts]) * np.timedelta64(1, "s")
        frames.append(np.asarray(merged, dtype=np.float32))
        times.append(np.datetime64(tm, "ns"))
        names.append("+".join(Path(n).name for n, _ in pairs))
        notes += [w for w in rec.warnings if w not in notes]
    ny, nx = layout["shape"]
    lat0, lon0 = layout["origin"]
    seq = nf.FrameSequence(
        times=np.asarray(times),
        data=np.stack(frames),
        field=nf.FIELD_REFLECTIVITY
        if str(options.get("variable") or "DBZH").upper().startswith("DBZ")
        else nf.FIELD_RAIN_RATE,
        cell_m=float(layout["cell_m"]),
        x=layout["x0"] + np.arange(nx) * layout["cell_m"],
        y=layout["y0"] + np.arange(ny) * layout["cell_m"],
        origin=(float(lat0), float(lon0)),
        proj4=nf.aeqd_proj4(lat0, lon0),
        covered=covered,
        sources=names,
        radars=radars,
        notes=notes,
        gridding={
            "engine": "mosaic (radarx)",
            "product": options.get("product") or "composite",
            "variable": options.get("variable") or "DBZH",
            "resolution_m": grid["resolution_m"],
            "max_range_km": grid["max_range_km"],
            "group_s": group_s,
            "bbox": list(layout["bbox"]),
        },
    )
    return seq


def _layers(
    nc: nr.Nowcast, options: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[list[float]] | None]:
    """Map PNGs of the deterministic field and of one probability, per lead."""
    from engine import mosaic as me

    bbox = nc.frames.gridding.get("bbox")
    if not bbox:
        return [], None
    layout = me._layout(tuple(bbox), nc.frames.cell_m)
    is_refl = nc.field == "reflectivity"
    lut, _ = me._build_lut(me._CMAPS.get("DBZH", me._FALLBACK_CMAP) if is_refl else "viridis")
    vmin, vmax = (0.0, 65.0) if is_refl else (0.0, 50.0)
    plut, _ = me._build_lut("magma")
    want = options.get("layers") or "both"
    thr = nc.thresholds[1] if len(nc.thresholds) > 1 else nc.thresholds[0]
    out: list[dict[str, Any]] = []
    rec = me.MosaicRecord.__new__(me.MosaicRecord)  # only for corners()
    rec.bbox = layout.bbox
    for k, ld in enumerate(nc.leads_min):
        if len(out) >= MAX_LAYERS:
            break
        if want in ("both", "deterministic"):
            img = me._resample_to_image(nc.deterministic[k], layout)
            png = me.render_png(img, lut, vmin, vmax)
            out.append(
                {
                    "lead_min": float(ld),
                    "kind": "deterministic",
                    "valid_time": prov.iso_utc(nc.valid_times[k]),
                    "png_b64": base64.b64encode(png).decode("ascii"),
                    "vmin": vmin,
                    "vmax": vmax,
                    "units": nc.units,
                    "legend": me._legend(lut, vmin, vmax),
                    "label": f"Nowcast +{ld:g} min [FORECAST]",
                }
            )
        if want in ("both", "probability") and thr in nc.probability:
            img = me._resample_to_image(nc.probability[thr][k], layout)
            png = me.render_png(img, plut, 0.0, 1.0)
            out.append(
                {
                    "lead_min": float(ld),
                    "kind": "probability",
                    "threshold": float(thr),
                    "valid_time": prov.iso_utc(nc.valid_times[k]),
                    "png_b64": base64.b64encode(png).decode("ascii"),
                    "vmin": 0.0,
                    "vmax": 1.0,
                    "units": "1",
                    "legend": me._legend(plut, 0.0, 1.0),
                    "label": f"P(>= {thr:g} {nc.units}) +{ld:g} min [FORECAST]",
                }
            )
    return out, rec.corners


def run(
    msg: dict[str, Any], readers: list[tuple[str, Any]], progress: Any, cancel: Any = None
) -> dict[str, Any]:
    """Execute ``nowcast_run``; returns the ``nowcast_result`` message (without type / id)."""
    options = msg.get("options") or {}
    if not isinstance(options, dict):
        raise nr.NowcastError("nowcast_run: 'options' must be an object")
    progress(3, "Gridding the frames")
    seq = build_frames(readers, options, progress, cancel)
    origin = seq.index_at(options["origin"]) if options.get("origin") else None
    n_members = int(_num(options, "n_members", 0))
    nc = nr.nowcast(
        seq,
        lead_min=_num(options, "lead_min", 60.0),
        step_min=_num(options, "step_min", 5.0),
        n_members=n_members,
        motion_method=str(options.get("motion_method") or "lucas_kanade"),
        n_frames=int(_num(options, "n_frames", 3)),
        origin_index=origin,
        thresholds=_thresholds(options, "thresholds"),
        rate_thresholds=_thresholds(options, "rate_thresholds") or (0.5, 2.0, 10.0),
        zr_relation=str(options.get("zr_relation") or "wsr88d"),
        decay=options.get("decay") or "persistence",
        seed=int(_num(options, "seed", 0)),
        keep_members=False,
        progress=lambda p, m: progress(50 + p // 2, m),
        cancel=cancel,
    )
    nid = STORE.put(nc)
    layers, corners = _layers(nc, options)
    from engine.api_results import jsonable

    return {
        "nowcast_id": nid,
        "origin_time": prov.iso_utc(nc.origin_time),
        "leads_min": [float(v) for v in nc.leads_min],
        "valid_times": [prov.iso_utc(t) for t in nc.valid_times],
        "field": nc.field,
        "units": nc.units,
        "grid": {
            "nx": seq.shape[1],
            "ny": seq.shape[0],
            "cell_m": seq.cell_m,
            "origin": list(seq.origin) if seq.origin else None,
            "bbox": seq.gridding.get("bbox"),
        },
        "corners": corners,
        "motion": nc.motion.to_dict(),
        "stats": jsonable(nc.stats()),
        "notes": nc.notes,
        "provenance": nc.provenance(),
        "record": jsonable(nc.record()),
        "layers": layers,
    }


def export(msg: dict[str, Any]) -> Path:
    nc = STORE.get(msg.get("nowcast_id"))
    path = msg.get("path")
    if not isinstance(path, str) or not path:
        raise nr.NowcastError("nowcast_export: 'path' is required")
    target = Path(path).expanduser()
    if target.exists() and target.suffix and not msg.get("overwrite"):
        raise nr.NowcastError(f"{target.name} already exists")
    return nc.save(target)[0]

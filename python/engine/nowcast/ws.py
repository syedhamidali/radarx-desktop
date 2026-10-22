"""WebSocket messages of the nowcast (the server only forwards them here).

Protocol (JSON; a request carries ``request_id`` that the reply echoes)::

    -> {type: "nowcast_run", request_id, file_ids: [...open files in time order...]
        | mosaic_ids: [...the engine's recent composites (the live map's frames)...]
        | frames_path: "gridded frames .nc" (desktop only),
        options: {lead_min, step_min, n_members, motion_method, n_frames, origin,
                  resolution_m, max_range_km, extent, variable, product, group_s,
                  zr_relation, decay, seed, thresholds, rate_thresholds, layers}}
    <- progress ..., {type: "nowcast_result", request_id, nowcast_id, origin_time, leads_min,
        valid_times, field, units, grid, corners, motion, stats, notes, provenance, record,
        layers: [{lead_min, kind: "deterministic"|"probability", threshold?, png_b64, vmin,
        vmax, legend, label}]}
    <- {type: "nowcast_error", request_id, message}
    -> {type: "nowcast_cancel", request_id}                 (stops the run at its next check)
    -> {type: "nowcast_layers", request_id, nowcast_id, kind: "deterministic"|"probability",
        threshold?, rate?}   <- {type: "nowcast_layers_result", request_id, nowcast_id, kind,
        threshold, rate, layers, corners}     (the map PNGs of one product for every lead)
    -> {type: "nowcast_export", request_id, nowcast_id, path, overwrite?}   <- {type: "file_saved"}

``layers`` are map-ready PNGs on the same Web-Mercator image grid as a mosaic (with ``corners``),
so the existing map layer code can draw them; ``provenance`` is the ``forecast`` layer
provenance (method, lead, skill statement, the guidance sentence).
"""

from __future__ import annotations

import base64
import math
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
MAX_LAYERS = 125  # 120 leads of one product


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


def _volume_time(rec: Any) -> np.datetime64 | None:
    """Mean volume time of the radars merged into a composite (else its analysis time)."""
    ts = []
    for r in getattr(rec, "radars", []) or []:
        v = getattr(r, "volume_time", None)
        if v:
            try:
                ts.append(np.datetime64(str(v).rstrip("Z"), "ns"))
            except ValueError:
                continue
    if ts:
        t0 = min(ts)
        return t0 + np.mean([(t - t0) / np.timedelta64(1, "s") for t in ts]) * np.timedelta64(
            1, "s"
        )
    at = getattr(rec, "analysis_time", None)
    if at:
        try:
            return np.datetime64(str(at).rstrip("Z"), "ns")
        except ValueError:
            return None
    return None


def frames_from_mosaics(records: list[Any], options: dict[str, Any]) -> nf.FrameSequence:
    """Frames from the engine's recent composites (the live map's frames), which keep their
    merged field on the working grid. They must share one grid and be reflectivity."""
    if len(records) < 1:
        raise nr.NowcastError("no composites given")
    stamped = []
    for rec in records:
        if rec.merged_aeqd is None:
            raise nr.NowcastError("a composite has no working-grid field: build it again")
        if not str(rec.variable).upper().startswith("DBZ"):
            raise nr.NowcastError(
                f"composite {rec.mosaic_id} is {rec.variable}: the nowcast needs reflectivity"
            )
        t = _volume_time(rec)
        if t is None:
            raise nr.NowcastError(f"composite {rec.mosaic_id} has no scan time")
        stamped.append((t, rec))
    stamped.sort(key=lambda p: p[0])
    first = stamped[0][1]
    for _, rec in stamped[1:]:
        if (
            rec.merged_aeqd.shape != first.merged_aeqd.shape
            or tuple(rec.origin) != tuple(first.origin)
            or rec.cell_m != first.cell_m
            or rec.x0 != first.x0
            or rec.y0 != first.y0
        ):
            raise nr.NowcastError(
                "the composites are on different grids (different radars or extent): "
                "the nowcast needs one grid"
            )
    ny, nx = first.merged_aeqd.shape
    lat0, lon0 = first.origin
    covered = first.covered_aeqd
    radars: list[dict[str, Any]] = []
    names, notes = [], []
    for _, rec in stamped:
        if rec.covered_aeqd is not None and covered is not None:
            covered = covered | rec.covered_aeqd
        for r in rec.radars:
            if r.name not in {x["name"] for x in radars}:
                radars.append({"name": r.name, "latitude": r.latitude, "longitude": r.longitude})
        names.append(f"composite {_iso(_volume_time(rec))}")
        notes += [w for w in rec.warnings if w not in notes]
    return nf.FrameSequence(
        times=np.asarray([t for t, _ in stamped]),
        data=np.stack([np.asarray(r.merged_aeqd, dtype=np.float32) for _, r in stamped]),
        field=nf.FIELD_REFLECTIVITY,
        cell_m=float(first.cell_m),
        x=first.x0 + np.arange(nx) * first.cell_m,
        y=first.y0 + np.arange(ny) * first.cell_m,
        origin=(float(lat0), float(lon0)),
        proj4=nf.aeqd_proj4(lat0, lon0),
        covered=covered,
        sources=names,
        radars=radars,
        notes=notes,
        gridding={
            "engine": "recent composites (live map)",
            "product": first.product,
            "variable": first.variable,
            "resolution_m": float(first.cell_m),
            "max_range_km": first.grid_info.get("max_range_km"),
            "bbox": [float(v) for v in first.bbox],
        },
    )


def _iso(t: Any) -> str:
    return prov.iso_utc(t) if t is not None else "unknown time"


def frames_from_file(paths: Any, options: dict[str, Any]) -> nf.FrameSequence:
    """Frames from gridded NetCDF the engine wrote earlier (desktop only: a path on this
    computer)."""
    if isinstance(paths, str):
        paths = [paths]
    if (
        not isinstance(paths, (list, tuple))
        or not paths
        or not all(isinstance(x, str) for x in paths)
    ):
        raise nr.NowcastError("nowcast_run: 'frames_path' must be a path or a list of paths")
    files = [Path(x).expanduser() for x in paths]
    for f in files:
        if not f.is_file():
            raise nr.NowcastError(f"{f.name}: no such file")
    try:
        return nf.from_netcdf(
            [str(f) for f in files],
            variable=options.get("variable") or None,
        )
    except nf.FrameError as exc:
        raise nr.NowcastError(str(exc)) from exc


def _layout_for(nc: nr.Nowcast) -> Any:
    """The image layout of the nowcast grid (the frames' own AEQD axes, any source)."""
    from engine import mosaic as me

    fr = nc.frames
    if fr.origin is None:
        raise nr.NowcastError("the frames have no map origin: they cannot be drawn on the map")
    bbox = fr.gridding.get("bbox")
    if not bbox:
        import pyproj

        inv = pyproj.Transformer.from_crs(
            fr.proj4 or nf.aeqd_proj4(*fr.origin), 4326, always_xy=True
        )
        ex = np.concatenate([fr.x, fr.x, np.full(fr.y.size, fr.x[0]), np.full(fr.y.size, fr.x[-1])])
        ey = np.concatenate([np.full(fr.x.size, fr.y[0]), np.full(fr.x.size, fr.y[-1]), fr.y, fr.y])
        lon, lat = inv.transform(ex, ey)
        bbox = [float(np.min(lon)), float(np.min(lat)), float(np.max(lon)), float(np.max(lat))]
    west, south, east, north = (float(v) for v in bbox)
    lat_c = 0.5 * (south + north)
    px = fr.cell_m / math.cos(math.radians(lat_c))
    nx_img = max(2, math.ceil(me.MERC_R * math.radians(east - west) / px))
    ny_img = max(2, math.ceil(float(me._merc_y(north) - me._merc_y(south)) / px))
    if nx_img * ny_img > me.MAX_IMAGE_PIXELS:
        raise nr.NowcastError("the nowcast image would be too large to draw; use a coarser grid")
    return me._Layout(
        (west, south, east, north),
        (float(fr.origin[0]), float(fr.origin[1])),
        np.asarray(fr.x, dtype=np.float64),
        np.asarray(fr.y, dtype=np.float64),
        float(fr.cell_m),
        nx_img,
        ny_img,
    )


def _closest(table: dict[float, Any], value: float | None, what: str) -> float:
    if not table:
        raise nr.NowcastError(f"this nowcast has no {what} probabilities")
    if value is None:
        keys = sorted(table)
        return keys[1] if len(keys) > 1 else keys[0]
    best = min(table, key=lambda k: abs(k - float(value)))
    if abs(best - float(value)) > 1e-6 * max(1.0, abs(best)):
        raise nr.NowcastError(f"no probability of {float(value):g} for this nowcast")
    return best


def layers_for(
    nc: nr.Nowcast,
    kind: str = "deterministic",
    threshold: float | None = None,
    *,
    rate: bool = False,
) -> tuple[list[dict[str, Any]], list[list[float]]]:
    """Map PNGs of one product for every lead: the deterministic field, or the probability of
    exceeding ``threshold`` (``rate=True``: a rain rate in mm/h, else the input quantity)."""
    from engine import mosaic as me

    if kind not in ("deterministic", "probability"):
        raise nr.NowcastError("nowcast layers: kind must be 'deterministic' or 'probability'")
    layout = _layout_for(nc)
    is_refl = nc.field == "reflectivity"
    if kind == "deterministic":
        lut, _ = me._build_lut(me._CMAPS.get("DBZH", me._FALLBACK_CMAP) if is_refl else "viridis")
        vmin, vmax = (0.0, 65.0) if is_refl else (0.0, 50.0)
        stack, thr, units = nc.deterministic, None, nc.units
    else:
        lut, _ = me._build_lut("magma")
        vmin, vmax = 0.0, 1.0
        table = nc.rate_probability if rate else nc.probability
        thr = _closest(table, threshold, "rain-rate" if rate else "exceedance")
        stack, units = table[thr], "1"
    q_units = "mm/h" if rate else nc.units
    rec = me.MosaicRecord.__new__(me.MosaicRecord)  # only for corners()
    rec.bbox = layout.bbox
    out: list[dict[str, Any]] = []
    for k, ld in enumerate(nc.leads_min):
        if len(out) >= MAX_LAYERS:
            break
        img = me._resample_to_image(stack[k], layout)
        png = me.render_png(img, lut, vmin, vmax)
        item: dict[str, Any] = {
            "lead_min": float(ld),
            "kind": kind,
            "valid_time": prov.iso_utc(nc.valid_times[k]),
            "png_b64": base64.b64encode(png).decode("ascii"),
            "vmin": vmin,
            "vmax": vmax,
            "units": units,
            "legend": me._legend(lut, vmin, vmax),
        }
        if kind == "deterministic":
            item["label"] = f"Nowcast +{ld:g} min [FORECAST]"
        else:
            item["threshold"] = float(thr)
            item["rate"] = bool(rate)
            item["label"] = f"P(>= {thr:g} {q_units}) +{ld:g} min [FORECAST]"
        out.append(item)
    return out, [list(c) for c in rec.corners]


def _layers(
    nc: nr.Nowcast, options: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[list[float]] | None]:
    """Map PNGs of the deterministic field and of one probability, per lead (``layers`` option:
    ``both`` (default), ``deterministic``, ``probability`` or ``none``)."""
    want = options.get("layers") or "both"
    if want == "none":
        return [], _layout_corners(nc)
    out: list[dict[str, Any]] = []
    corners: list[list[float]] = []
    if want in ("both", "deterministic"):
        out, corners = layers_for(nc, "deterministic")
    if want in ("both", "probability") and nc.probability:
        more, corners = layers_for(nc, "probability", None)
        out = sorted(out + more, key=lambda x: x["lead_min"]) if out else more
    return out[:MAX_LAYERS], corners or None


def _layout_corners(nc: nr.Nowcast) -> list[list[float]]:
    from engine import mosaic as me

    rec = me.MosaicRecord.__new__(me.MosaicRecord)
    rec.bbox = _layout_for(nc).bbox
    return [list(c) for c in rec.corners]


def layers(msg: dict[str, Any]) -> dict[str, Any]:
    """Execute ``nowcast_layers``: one product's PNGs for every lead of a stored nowcast."""
    nc = STORE.get(msg.get("nowcast_id"))
    kind = str(msg.get("kind") or "deterministic")
    thr = msg.get("threshold")
    if thr is not None and (isinstance(thr, bool) or not isinstance(thr, (int, float))):
        raise nr.NowcastError("nowcast_layers: 'threshold' must be a number")
    lay, corners = layers_for(
        nc, kind, None if thr is None else float(thr), rate=bool(msg.get("rate"))
    )
    return {
        "nowcast_id": msg["nowcast_id"],
        "kind": kind,
        "threshold": lay[0].get("threshold") if lay else thr,
        "rate": bool(msg.get("rate")),
        "layers": lay,
        "corners": corners,
    }


def run(
    msg: dict[str, Any],
    readers: list[tuple[str, Any]],
    progress: Any,
    cancel: Any = None,
    mosaics: list[Any] | None = None,
) -> dict[str, Any]:
    """Execute ``nowcast_run``; returns the ``nowcast_result`` message (without type / id).

    The frames come from open volumes (``readers``), from the engine's recent composites
    (``mosaics``) or from a gridded file (``msg["frames_path"]``)."""
    options = msg.get("options") or {}
    if not isinstance(options, dict):
        raise nr.NowcastError("nowcast_run: 'options' must be an object")
    progress(3, "Gridding the frames")
    if readers:
        seq = build_frames(readers, options, progress, cancel)
    elif mosaics:
        seq = frames_from_mosaics(mosaics, options)
    elif msg.get("frames_path"):
        seq = frames_from_file(msg["frames_path"], options)
    else:
        raise nr.NowcastError("nowcast_run: no open files, composites or frames file given")
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
            "bbox": list(_layout_for(nc).bbox),
        },
        "corners": corners,
        "n_members": nc.n_members,
        "thresholds": [float(v) for v in nc.thresholds],
        "rate_thresholds": [float(v) for v in nc.rate_thresholds],
        "sources": list(seq.sources[-int(_num(options, "n_frames", 3)) :]),
        "n_input_frames": seq.n,
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

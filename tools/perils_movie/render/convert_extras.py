"""The parts of the PERiLS bundle (SPEC v1 and its v1.1 additions, docs/PERILS_BUNDLE.md) beyond the
radar volume and the lightning: tornado survey, rotation tracks, stations with their hail events and
drop-size spectra, multi-Doppler winds, MRR / wind-profiler time-height fields and the science events.
Each function is tolerant to the shapes the data tools write (a list or a dict with the list inside;
a point as a list or as an object) and returns what the clip page needs, in the scene frame (km).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np


def _json(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ── tornadoes ───────────────────────────────────────────────────────────────


def convert_tornadoes(
    raw, geo, rel, window_s: float | None = None, bounds_km: float | None = None
) -> list[dict]:
    """Survey records: ``path`` points are [t_s, lat, lon] or {t_s, lat, lon}; ``path_kind`` says how they were made.

    The survey of a whole day lists many tornadoes. With ``window_s`` and ``bounds_km`` only those that are alive
    inside the window AND have a point inside the shown area (+- ``bounds_km`` around the origin) are kept, the one
    with the longest time inside the window first.
    """
    if isinstance(raw, dict):
        raw = raw.get("tornadoes", [])
    out = []
    for tor in raw or []:
        path = []
        for q in tor.get("path", []):
            if isinstance(q, (list, tuple)):
                t, lat, lon = float(q[0]), float(q[1]), float(q[2])
            else:
                t = float(q["t_s"]) if "t_s" in q else rel(q["time"])
                lat, lon = float(q["lat"]), float(q["lon"])
            x, y = geo.xy(lat, lon)
            path.append({"t": t, "x": float(x), "y": float(y)})
        keep = {
            k: v
            for k, v in tor.items()
            if k not in ("path", "start", "end", "begin", "end_point", "survey_narrative_start")
        }
        keep["start_iso"] = tor.get("start") if isinstance(tor.get("start"), str) else None
        keep["end_iso"] = tor.get("end") if isinstance(tor.get("end"), str) else None
        keep["path_interpolated"] = (
            "interpolated" in str(tor.get("path_kind", "")).lower() or tor.get("path_kind") is None
        )
        if window_s is not None and path:
            ts = [q["t"] for q in path]
            inside_t = min(ts[-1], window_s) - max(ts[0], 0.0)
            near = bounds_km is None or any(
                abs(q["x"]) <= bounds_km and abs(q["y"]) <= bounds_km for q in path
            )
            if inside_t < 0 or not near:
                continue
            keep["seconds_in_window"] = max(0.0, inside_t)
        out.append({**keep, "path": path})
    out.sort(key=lambda t: -t.get("seconds_in_window", 0.0))
    return out


# ── rotation tracks ─────────────────────────────────────────────────────────

_VROT_KEYS = ("vrot_ms", "vrot", "vrot_max_ms", "max_vrot_ms", "peak_vrot_ms", "vrot_ll")
_AS_KEYS = ("peak_as", "as_peak", "max_as", "az_shear_s-1")


def _xy_of(q, geo):
    if "x_km" in q and "y_km" in q:
        return float(q["x_km"]), float(q["y_km"])
    if "x" in q and "y" in q:
        return float(q["x"]), float(q["y"])
    if "lat" in q and "lon" in q:
        x, y = geo.xy(q["lat"], q["lon"])
        return float(x), float(y)
    return None


def convert_rotation(raw, geo, rel) -> list[dict]:
    """Vortex tracks of the radar rotation analysis: a list of tracks, or {"tracks": [...]} (each with "points")."""
    if raw is None:
        return []
    tracks = raw.get("tracks", []) if isinstance(raw, dict) else raw
    default_radar = raw.get("radar") if isinstance(raw, dict) else None
    out = []
    for tr in tracks:
        pts = []
        for q in tr.get("points", []):
            xy = _xy_of(q, geo)
            if xy is None:
                continue
            t = float(q["t_s"]) if "t_s" in q else (rel(q["time"]) if "time" in q else None)
            if t is None:
                continue
            rec = {"t": t, "x": xy[0], "y": xy[1]}
            for k in _VROT_KEYS:
                if _num(q.get(k)) is not None:
                    rec["vrot_ms"] = _num(q[k])
                    break
            for k in _AS_KEYS:
                if _num(q.get(k)) is not None:
                    rec["as"] = _num(q[k])
                    break
            if "tds" in q:
                rec["tds"] = bool(q["tds"])
            pts.append(rec)
        if pts:
            out.append(
                {
                    "id": tr.get("id"),
                    "radar": tr.get("radar", default_radar),
                    "resolution_m": tr.get("resolution_m"),
                    "points": pts,
                }
            )
    return out


# ── stations ────────────────────────────────────────────────────────────────


def _series(s):
    out = {}
    for k, v in (s or {}).items():
        out[k] = [
            None if (x is None or (isinstance(x, float) and not math.isfinite(x))) else float(x)
            for x in v
        ]
    return out


_KIND = (
    ("PIPS", "PIPS disdrometer"),
    ("StickNet", "StickNet"),
    ("RaDAPS", "RaDAPS station"),
    ("Mesonet", "mobile mesonet"),
    ("FARM", "FARM Parsivel"),
)


def station_label(sid: str, typ: str) -> tuple[str, str]:
    """A readable name and a short kind for a station id like ``StickNet_0102A``."""
    kind = next(
        (k for pre, k in _KIND if sid.startswith(pre)), (typ or "station").split(" (")[0][:28]
    )
    return sid.replace("_", " "), kind


def convert_stations(raw, geo, bundle: Path, hail_events, log=print) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("stations", [])
    ev_by: dict[str, list] = {}
    for e in hail_events or []:
        ev_by.setdefault(str(e.get("station")), []).append(
            {k: v for k, v in e.items() if k != "station"}
        )
    out = []
    for s in raw or []:
        x, y = geo.xy(s["lat"], s["lon"])
        label, kind = station_label(str(s["id"]), s.get("type", ""))
        rec = {
            "id": str(s["id"]),
            "name": s.get("name", label),
            "type": kind,
            "typeLong": s.get("type", ""),
            "moving": bool(s.get("moving")),
            "x": float(x),
            "y": float(y),
            "lat": s["lat"],
            "lon": s["lon"],
            "altKm": (s.get("alt_m") or 0.0) / 1000.0,
            "series": _series(s.get("series")),
            "events": ev_by.get(str(s["id"]), []) + list(s.get("events") or []),
            "credit": s.get("credit"),
        }
        if s.get("dsd"):
            d = convert_dsd(bundle / s["dsd"])
            if d:
                rec["dsd"] = d
        ser = s.get("series") or {}
        if ser.get("lat") and ser.get("lon") and ser.get("t_s"):
            # a vehicle: its track (every 10 s), and it counts as moving if it covers more than 2 km
            lat = np.asarray([np.nan if v is None else v for v in ser["lat"]], float)
            lon = np.asarray([np.nan if v is None else v for v in ser["lon"]], float)
            ts = np.asarray(ser["t_s"], float)
            ok = np.isfinite(lat) & np.isfinite(lon)
            if ok.sum() > 2:
                tx, ty = geo.xy(lat[ok], lon[ok])
                keep = np.arange(
                    0,
                    int(ok.sum()),
                    max(1, round(10.0 / max(1e-6, np.median(np.diff(ts[ok]))))),
                )
                rec["track"] = [
                    [round(float(ts[ok][i]), 1), round(float(tx[i]), 3), round(float(ty[i]), 3)]
                    for i in keep
                ]
                span = float(np.hypot(np.ptp(tx), np.ptp(ty)))
                rec["moving"] = rec["moving"] or span > 2.0
                rec["trackSpanKm"] = round(span, 2)
        # the series that the page does not draw as such (the position of a vehicle) are not repeated
        rec["series"] = {k: v for k, v in rec["series"].items() if k not in ("lat", "lon")}
        out.append(rec)
    return out


def convert_dsd(path: Path, max_cols: int = 200):
    """The drop-size spectrum of a disdrometer over time: log10 N(D) (m-3 mm-1) per time column and diameter bin."""
    if not path.exists():
        return None
    z = np.load(path)
    if "nd_m3mm" not in z.files:
        return None
    t = np.asarray(z["t_s"], float)
    d = np.asarray(z["diameter_mm"], float)
    nd = np.asarray(z["nd_m3mm"], float)
    stride = max(1, math.ceil(len(t) / max_cols))
    ts, nds = t[::stride], nd[::stride]
    logn = np.where(nds > 0, np.log10(np.maximum(nds, 1e-9)), np.nan)
    out = {
        "t": [round(float(v), 1) for v in ts],
        "d": [round(float(v), 3) for v in d],
        "logN": [[None if not np.isfinite(v) else round(float(v), 1) for v in row] for row in logn],
    }
    for k in ("rain_rate_mmh", "dm_mm"):
        if k in z.files:
            out[k] = [
                None if not np.isfinite(v) else round(float(v), 3)
                for v in np.asarray(z[k], float)[::stride]
            ]
    if "fallspeed_ms" in z.files:
        out["fall"] = [round(float(v), 2) for v in np.asarray(z["fallspeed_ms"], float)]
    return out


# ── multi-Doppler winds ─────────────────────────────────────────────────────


def convert_winds(
    npz_path: Path, k: int, out: Path, geo_origin, z_km, vizcfg=None
) -> tuple[dict | None, dict | None]:
    """One step of multi-Doppler winds -> the clip bundle's wind files (u then v, float32) and the visual layers.

    Returns (wind file refs, viz) where viz holds storm-relative horizontal arrows near ``vizcfg['z_km']`` and
    the strongest vertical-motion cells. The retrieval is masked where fewer than two radars see a cell or the beams
    cross at a small angle (NaN in the file); masked cells are never drawn.
    """
    z = np.load(npz_path)
    if "u" not in z.files or "v" not in z.files:
        return None, None
    u = np.asarray(z["u"], np.float32)
    v = np.asarray(z["v"], np.float32)
    nz, ny, nx = u.shape
    xk = np.asarray(z["x_km"], float) if "x_km" in z.files else np.linspace(-90, 90, nx)
    yk = np.asarray(z["y_km"], float) if "y_km" in z.files else np.linspace(-90, 90, ny)
    dx = float(xk[1] - xk[0])
    zz = np.asarray(z_km, float)
    z0 = float(zz[0])
    dz = float((zz[-1] - zz[0]) / max(1, len(zz) - 1))
    if len(zz) != nz:
        dz = float((zz[-1] - zz[0]) / max(1, nz - 1))
    hdr = {
        "nx": nx,
        "ny": ny,
        "nz": nz,
        "x0_km": float(xk[0]),
        "y0_km": float(yk[0]),
        "z0_km": z0,
        "dx_km": dx,
        "dy_km": float(yk[1] - yk[0]),
        "dz_km": dz,
        "origin": {"lat": geo_origin[0], "lon": geo_origin[1]},
        "n_radars": int(np.nanmax(z["nradars"])) if "nradars" in z.files else 2,
        "valid_fraction": float(np.isfinite(u).mean()),
    }
    (out / f"wind_{k}.json").write_text(json.dumps(hdr))
    (out / f"wind_{k}.bin").write_bytes(
        np.ascontiguousarray(np.concatenate([u.ravel(), v.ravel()])).astype("<f4").tobytes()
    )
    ref = {"header": f"wind_{k}.json", "bin": f"wind_{k}.bin"}
    # visual layers
    cfg = vizcfg or {}
    zk = float(cfg.get("z_km", 1.5))
    kz = int(np.clip(round((zk - z0) / dz), 0, nz - 1))
    us = np.asarray(z["u_sr"], np.float32) if "u_sr" in z.files else u
    vs = np.asarray(z["v_sr"], np.float32) if "v_sr" in z.files else v
    st = int(cfg.get("stride", 5))
    glyph = []
    for j in range(0, ny, st):
        for i in range(0, nx, st):
            a, b = us[kz, j, i], vs[kz, j, i]
            if np.isfinite(a) and np.isfinite(b):
                glyph.append(
                    [
                        round(float(xk[i]), 2),
                        round(float(yk[j]), 2),
                        round(float(a), 1),
                        round(float(b), 1),
                    ]
                )
    cores = []
    if "w" in z.files:
        w = np.asarray(z["w"], np.float32)
        thr = float(cfg.get("w_thr", 5.0))
        sel = np.isfinite(w) & (np.abs(w) >= thr)
        kk, jj, ii = np.nonzero(sel)
        if len(kk) > 1500:
            pick = np.random.default_rng(3).choice(len(kk), 1500, replace=False)
            kk, jj, ii = kk[pick], jj[pick], ii[pick]
        for a, b, c in zip(kk, jj, ii, strict=True):
            cores.append(
                [
                    round(float(xk[c]), 2),
                    round(float(yk[b]), 2),
                    round(float(z0 + a * dz), 2),
                    round(float(w[a, b, c]), 1),
                ]
            )
    return ref, {"zKm": float(z0 + kz * dz), "glyphs": glyph, "w": cores}


# ── profiles (MRR / wind profiler) ──────────────────────────────────────────


def convert_profiles(files: dict, geo=None, site=None, max_cols: int = 600) -> dict:
    """Time-height fields as small tables: {name: {"t": [...], "h": [...], "fields": {key: {"min","max","codes": [[...]]}}}}."""
    out = {}
    for name, rel_path in (files or {}).items():
        z = np.load(rel_path, allow_pickle=True)
        if "t_s" not in z.files or "height_m" not in z.files:
            continue
        t = np.asarray(z["t_s"], float)
        h = np.asarray(z["height_m"], float)
        stride = max(1, math.ceil(len(t) / max_cols))
        fields = {}
        for key in ("dbz", "fall_speed_ms", "rain_rate_mmh", "u", "v", "w", "speed_ms", "snr_db"):
            if key not in z.files:
                continue
            a = np.asarray(z[key], float)[::stride]
            if a.ndim != 2 or a.shape[0] != len(t[::stride]):
                continue
            fin = a[np.isfinite(a)]
            if not fin.size:
                continue
            lo, hi = float(np.percentile(fin, 1)), float(np.percentile(fin, 99))
            if hi <= lo:
                hi = lo + 1.0
            codes = np.where(
                np.isfinite(a), np.clip(np.rint((a - lo) / (hi - lo) * 254) + 1, 1, 255), 0
            ).astype(int)
            fields[key] = {"min": round(lo, 3), "max": round(hi, 3), "codes": codes.tolist()}
        out[name] = {
            "t": [round(float(v), 1) for v in t[::stride]],
            "h": [round(float(v), 1) for v in h],
            "fields": fields,
        }
    return out


# ── science events ──────────────────────────────────────────────────────────


_KIND_ALIAS = {
    "gust_front_passage": "gust_front",
    "first_lma_flash": "first_flash",
    "lma_rate": "flash_rate",
    "tornado_survey": "tornado",
    "rear_inflow_candidate": "rear_inflow_jet",
    "low_level_vorticity_maximum": "vorticity",
    "updraft_maximum": "updraft",
    "tornado_radar_rotation": "rotation",
    "low_level_convergence": "convergence",
    "tornado_rotation_kinematics": "tornado_rotation",
    "low_level_winds_at_tornado": "winds_at_tornado",
    "lightning_near_tornado": "lightning_near_tornado",
}


def science_kind(kind: str, evidence: dict) -> str:
    """The film's name of a data event kind (the data tool's ``hail_signature`` covers three different things)."""
    if kind == "hail_signature":
        if "zdr_column_max_km_series" in evidence:
            return "zdr_column"
        if "mesh_max_mm" in evidence:
            return "hail"
        return "debris_signature"
    return _KIND_ALIAS.get(kind, kind)


def convert_science(raw, rel, geo=None) -> list[dict]:
    """science_events.json -> events {t_s, kind, caption, class, where, x, y, evidence numbers}.

    Only events inside the window are kept (``in_window`` is false for context outside it). ``caption`` is the
    data tool's own sentence with its numbers; the film adds one sentence of physics per kind (perilsCaptions.ts).
    """
    if raw is None:
        return []
    items = raw.get("events", raw) if isinstance(raw, dict) else raw
    out = []
    for e in items:
        if not isinstance(e, dict):
            continue
        if e.get("in_window") is False:
            continue
        t = _num(e.get("t_s"))
        if t is None and e.get("time"):
            t = rel(e["time"])
        if t is None:
            continue
        ev = e.get("evidence") if isinstance(e.get("evidence"), dict) else {}
        nums = e.get("numbers")
        if isinstance(nums, dict):
            nums = [f"{k} {v}" for k, v in nums.items()]
        x, y = _num(ev.get("x_km")), _num(ev.get("y_km"))
        if (
            (x is None or y is None)
            and geo is not None
            and _num(ev.get("lat")) is not None
            and _num(ev.get("lon")) is not None
        ):
            gx, gy = geo.xy(ev["lat"], ev["lon"])
            x, y = float(gx), float(gy)
        cls = str(e.get("class") or e.get("provenance") or "")
        out.append(
            {
                "t_s": t,
                "kind": science_kind(str(e.get("kind") or e.get("type") or "event"), ev),
                "kindRaw": str(e.get("kind") or e.get("type") or "event"),
                "title": e.get("title"),
                "caption": e.get("caption"),
                "numbers": [str(n) for n in (nums or [])],
                "text": e.get("text") or e.get("summary"),
                "class": "retrieved"
                if cls.startswith("candidate")
                else cls.split(" ")[0]
                if cls
                else None,
                "candidate": cls.startswith("candidate"),
                "where": e.get("where") or e.get("station") or ev.get("station"),
                "x": x,
                "y": y,
                "lat": _num(e.get("lat")),
                "lon": _num(e.get("lon")),
            }
        )
    return sorted(out, key=lambda d: d["t_s"])

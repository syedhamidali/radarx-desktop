"""Render the PERiLS soundtrack: ``render_audio(bundle_dir, timeline, camera_path, out_wav, ...)``.

Everything is synthesised from the data, not a recording (see docs/PERILS_AUDIO.md).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from . import LABEL
from .acoustics import SPEED_OF_SOUND_MS
from .bundle import Bundle, load_bundle, parse_events
from .geo import enu
from .layers import render_ambient, render_convective, render_tornado
from .mix import PRIORITY, level_env_db, lufs, master, write_wav
from .scene import Scene
from .thunder import ThunderParams, add_at, plan_flash, render_flash, select_flashes
from .timeline import Timeline, load_camera, load_timeline
from .weather import render_hail, render_rain, render_wind

VERSION = 1
LAYER_NAMES = PRIORITY


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _load_events(events, bundle: Bundle, timeline_src):
    if events is None:
        cands = [Path(bundle.path) / "events.json"] if bundle.path else []
        if isinstance(timeline_src, str | Path):
            cands.append(Path(timeline_src).parent / "events.json")
        for c in cands:
            if c.exists():
                events = json.loads(c.read_text())
                break
    if events is None:
        return bundle.events
    if isinstance(events, str | Path):
        events = json.loads(Path(events).read_text())
    return parse_events(events, bundle.window_start)


def render_mix(
    bundle,
    timeline,
    camera_path,
    *,
    events=None,
    sample_rate: int = 48000,
    seed: int = 1,
    target_lufs: float = -23.0,
    thunder: ThunderParams | None = None,
    layers: set[str] | None = None,
    max_listener_height_m: float | None = 500.0,
    thunder_peak_dbfs: float | None = None,
    thunder_margin_db: float | None = 12.0,
):
    """Render in memory. Returns (stereo (2, N) float array, report dict, stems dict)."""
    sr = int(sample_rate)
    tl_src = timeline
    b = bundle if isinstance(bundle, Bundle) else load_bundle(bundle)
    tl = load_timeline(timeline, b.window_start)
    cam = load_camera(camera_path)
    cam_alt_max = float(cam.alt.max())
    ground = float(np.median([s.alt_m for s in b.stations])) if b.stations else 0.0
    if max_listener_height_m:
        # sound does not reach a camera 58 km up: the listener is heard from the ground below the camera,
        # never higher than this above the ground (the horizontal position and the heading are the camera's)
        cam = cam.capped(ground + float(max_listener_height_m))
    prm = thunder or ThunderParams()
    dur = float(tl.clip_duration_s)
    n = round(dur * sr)
    on = set(LAYER_NAMES) if layers is None else set(layers)
    ev = _load_events(events, b, tl_src)
    scene = Scene(b, tl, cam, dur, ev)

    raw: dict[str, np.ndarray] = {}
    info: dict[str, dict] = {}

    # thunder
    rows = [plan_flash(f, tl, cam, dur, prm) for f in b.flashes]
    _tornado_bonus(b, rows, prm)
    counts = select_flashes(rows, prm, dur)
    th = np.zeros((2, n))
    if "thunder" in on:
        for row in rows:
            if row["status"] != "rendered":
                continue
            air = scene.air_state(float(np.clip(row["t_flash_clip_s"], 0.0, dur)))
            start, x = render_flash(row, sr, seed, cam, prm, air)
            add_at(th, start, x)
    raw["thunder"] = th
    info["thunder"] = {"counts": counts}

    if "tornado" in on:
        raw["tornado"], info["tornado"] = render_tornado(scene, sr, n, seed + 11)
    else:
        raw["tornado"], info["tornado"] = np.zeros((2, n)), {}
    if "hail" in on:
        raw["hail"], info["hail"] = render_hail(scene, sr, n, seed + 23)
    else:
        raw["hail"], info["hail"] = np.zeros((2, n)), {}
    if "rain" in on:
        raw["rain"], info["rain"] = render_rain(scene, sr, n, seed + 37)
    else:
        raw["rain"], info["rain"] = np.zeros((2, n)), {}
    if "wind" in on:
        raw["wind"], info["wind"] = render_wind(scene, sr, n, seed + 41)
    else:
        raw["wind"], info["wind"] = np.zeros((2, n)), {}
    if "convective" in on:
        raw["convective"], info["convective"] = render_convective(scene, sr, n, seed + 53)
    else:
        raw["convective"], info["convective"] = np.zeros((2, n)), {}
    if "ambient" in on:
        raw["ambient"], info["ambient"] = render_ambient(sr, n, seed + 67)
    else:
        raw["ambient"], info["ambient"] = np.zeros((2, n)), {}

    # thunder has priority and a set margin: the other layers are trimmed (or raised) together until the median
    # voiced thunder stands ``thunder_margin_db`` above the rest of the mix (rms over its first 4 s)
    trim_db = 0.0
    for it in range(6):
        lay = {k: raw[k] * (1.0 if k == "thunder" else 10 ** (trim_db / 20.0)) for k in LAYER_NAMES}
        y, minfo, stems = master(lay, sr, target_lufs, thunder_peak_dbfs=thunder_peak_dbfs)
        _audit_thunder(rows, stems, sr, prm)
        margins = [r["margin_over_bed_db"] for r in rows if "margin_over_bed_db" in r]
        if not margins or thunder_margin_db is None or it == 5:
            break
        med = float(np.median(margins))
        if abs(med - thunder_margin_db) < 1.5:
            break
        trim_db = float(np.clip(trim_db - (thunder_margin_db - med), -24.0, 18.0))
    minfo["bed_trim_db"] = trim_db
    minfo["thunder_median_margin_over_bed_db"] = float(np.median(margins)) if margins else None

    per_layer = {}
    for k in LAYER_NAMES:
        s = stems[k]
        rms = float(np.sqrt(np.mean(s**2)))
        per_layer[k] = {
            "rms_dbfs": float(20 * np.log10(max(rms, 1e-12))),
            "lufs": lufs(s, sr) if rms > 1e-9 else None,
            "peak_dbfs": float(20 * np.log10(max(np.abs(s).max(), 1e-12))),
            "max_ducking_db": minfo["ducking_max_reduction_db"].get(k, 0.0),
            "levels_dbfs_per_s": [round(float(v), 1) for v in _per_second_db(s, sr)],
            **{kk: vv for kk, vv in info[k].items() if kk != "counts"},
        }
    # the flash table: full rows for the flashes that were considered for the mix; the ones that are out of
    # range or after the end of the clip keep only what identifies them (the report stays small)
    shown = [
        {
            k: v
            for k, v in _public(r).items()
            if k
            in (
                "id",
                "status",
                "n_sources",
                "t_flash_clip_s",
                "dist_nearest_m",
                "delay_nearest_clip_s",
            )
        }
        if r["status"] in ("skipped_out_of_range", "outside_clip")
        else _public(r)
        for r in rows
    ]
    provenance = _provenance(b, info, scene, rows)
    report = {
        "label": LABEL,
        "version": VERSION,
        "inputs": {
            "bundle": b.path,
            "window_start": b.window_start,
            "n_flashes": len(b.flashes),
            "n_stations": len(b.stations),
            "n_tornado_tracks": len(b.tornadoes),
            "n_events": len(ev),
            "radar_samples": b.radar_samples is not None,
        },
        "params": {
            "sample_rate": sr,
            "seed": seed,
            "target_lufs": target_lufs,
            "speed_of_sound_ms": SPEED_OF_SOUND_MS,
            "thunder": asdict(prm),
            "clip_duration_s": dur,
            "listener": {
                "max_height_above_ground_m": max_listener_height_m,
                "ground_alt_m": ground,
                "camera_altitude_max_m": cam_alt_max,
                "note": "the listener is the camera's ground track, at most this high above the ground",
            },
            "speedup": tl.speedup if tl.speedup is not None else "keyframes",
            "data_offset_s": tl.data_offset_s,
        },
        "master": minfo,
        "layers": per_layer,
        "thunder": {
            "counts": counts,
            "rendered": counts.get("rendered", 0),
            "skipped": {k: v for k, v in counts.items() if k != "rendered"},
            "selection": _selection_summary(rows, prm, dur),
            "loudest_events": _loudest(rows, 10),
            "level_model": (
                "level compressed for audibility (12 dB per decade of range instead of 20); the delay is "
                "physical (distance / 343 / speedup); every thunder is one real LMA flash"
                if prm.level_model == "audibility"
                else "physical 1/r level"
            ),
            "flashes": shown,
        },
        "provenance": provenance,
        "priority": PRIORITY,
        "assumptions": [
            "sound speed 343 m/s, constant; delay in the clip = distance / 343 / speedup",
            "thunder arrival of each LMA source = t_source + r / 343 (data time), mapped to clip time",
            "air absorption: ISO 9613-1 with assumed 80 % relative humidity, station T and p when present",
            "rain Dm assumed from the rain rate where the station has no Dm (see layers.rain.dm_mm)",
            "tornado roar, debris rattle and hail impacts: STYLISED timbre, position/time from the data",
            "listener = camera: positions at the flash time; heading at the sector's arrival time",
        ],
    }
    return y, report, stems


def _tornado_bonus(b: Bundle, rows: list[dict], prm: ThunderParams) -> None:
    """Score bonus for flashes within 10 km of a tornado that exists at the time of the flash."""
    for f, row in zip(b.flashes, rows, strict=True):
        row["near_tornado_km"] = None
        if row["status"] != "pending" or not np.isfinite(f.centroid[0]):
            continue
        best = None
        for t in b.tornadoes:
            la, lo, ex = t.position(f.t_start_s)
            if bool(ex):
                e, n, _ = enu(float(la), float(lo), 0.0, f.centroid[0], f.centroid[1], 0.0)
                d = float(np.hypot(e, n)) / 1000.0
                best = d if best is None else min(best, d)
        row["near_tornado_km"] = best
        if best is not None and best < 10.0:
            row["score_db"] += prm.tornado_bonus_db


def _audit_thunder(rows: list[dict], stems: dict, sr: int, prm: ThunderParams) -> None:
    """What each voiced thunder is in the final mix: its peak and level over the first seconds, and by how
    much it stands above everything else (the bed) in the same seconds."""
    th = stems["thunder"]
    bed = sum(v for k, v in stems.items() if k != "thunder")
    n = th.shape[1]
    for r in rows:
        if r["status"] != "rendered":
            continue
        a = max(0, int(r["first_arrival_clip_s"] * sr))
        z = min(n, int((min(r["first_arrival_clip_s"] + 4.0, r["last_arrival_clip_s"] + 2.0)) * sr))
        z = max(z, a + sr // 4)
        seg, bseg = th[:, a:z], bed[:, a:z]
        if seg.size == 0:
            continue
        rms_t = float(np.sqrt(np.mean(seg**2)))
        rms_b = float(np.sqrt(np.mean(bseg**2)))
        r["final_peak_dbfs"] = float(20 * np.log10(max(np.abs(seg).max(), 1e-12)))
        r["final_rms_dbfs"] = float(20 * np.log10(max(rms_t, 1e-12)))
        for name in ("rain", "tornado"):
            seg_o = stems[name][:, a:z]
            rms_o = float(np.sqrt(np.mean(seg_o**2)))
            r[f"margin_over_{name}_db"] = float(
                20 * np.log10(max(rms_t, 1e-12) / max(rms_o, 1e-12))
            )
        r["margin_over_bed_db"] = float(20 * np.log10(max(rms_t, 1e-12) / max(rms_b, 1e-12)))


def _selection_summary(rows: list[dict], prm: ThunderParams, dur: float) -> dict:
    """Which real flashes were voiced and how many were not, and the audibility guarantee per window."""
    heard = [
        r for r in rows if r["status"] not in ("skipped_out_of_range", "outside_clip", "no_sources")
    ]
    voiced = [r for r in rows if r["status"] == "rendered"]
    w = prm.guarantee_window_s
    wins = []
    for i in range(int(np.ceil(dur / w))):
        lo, hi = i * w, (i + 1) * w
        inwin = [r for r in heard if lo <= r["first_arrival_clip_s"] < hi]
        near = [r for r in inwin if r["dist_nearest_m"] <= prm.guarantee_range_m]
        v = [r for r in voiced if lo <= r["first_arrival_clip_s"] < hi]
        wins.append(
            {
                "clip_s": [lo, min(hi, dur)],
                "flashes_heard": len(inwin),
                "flashes_within_guarantee_range": len(near),
                "voiced": len(v),
                "best_voiced_margin_over_bed_db": max(
                    (x.get("margin_over_bed_db", -99.0) for x in v), default=None
                ),
            }
        )
    return {
        "real_flashes_in_bundle": len(rows),
        "voiced": len(voiced),
        "voiced_by": {
            k: sum(1 for r in voiced if r.get("selection") == k)
            for k in ("guaranteed", "guaranteed_distant", "score")
        },
        "heard_in_clip_and_range_but_not_voiced": len(heard) - len(voiced),
        "not_heard": len(rows) - len(heard),
        "guarantee": {
            "window_s": w,
            "range_m": prm.guarantee_range_m,
            "windows": wins,
            "windows_with_a_flash_in_range": sum(
                1 for x in wins if x["flashes_within_guarantee_range"]
            ),
            "windows_with_a_flash_in_range_and_voiced": sum(
                1 for x in wins if x["flashes_within_guarantee_range"] and x["voiced"]
            ),
            "windows_without_any_flash_heard": [
                x["clip_s"] for x in wins if x["flashes_heard"] == 0
            ],
        },
    }


def _loudest(rows: list[dict], n: int) -> list[dict]:
    voiced = sorted(
        (r for r in rows if r["status"] == "rendered"),
        key=lambda r: -r.get("final_peak_dbfs", -999.0),
    )
    keys = (
        "id",
        "first_arrival_clip_s",
        "t_flash_clip_s",
        "dist_nearest_m",
        "delay_nearest_clip_s",
        "n_sources",
        "final_peak_dbfs",
        "final_rms_dbfs",
        "margin_over_bed_db",
        "margin_over_rain_db",
        "margin_over_tornado_db",
        "selection",
        "near_tornado_km",
    )
    return [{k: r.get(k) for k in keys} for r in voiced[:n]]


def _provenance(b: Bundle, info: dict, scene: Scene, rows: list[dict]) -> dict:
    """Which station / instrument drove which layer and when (for the end card). Times are clip
    seconds and data seconds since the window start."""
    drivers = {
        "rain": info["rain"].get("drivers", []) + info["rain"].get("drop_spectrum_drivers", []),
        "wind": info["wind"].get("drivers", []),
    }
    used: dict[str, dict] = {}
    for layer, segs in drivers.items():
        for s in segs:
            u = used.setdefault(
                s["station_id"],
                {
                    "station_id": s["station_id"],
                    "name": s["station_name"],
                    "type": s["station_type"],
                    "network": s.get("network"),
                    "credit": s.get("credit"),
                    "layers": set(),
                    "clip_seconds": 0.0,
                },
            )
            u["layers"].add(layer)
            u["clip_seconds"] += s["clip_s"][1] - s["clip_s"][0]
    for row in info["hail"].get("rows", []):
        if row.get("station_id"):
            u = used.setdefault(
                row["station_id"],
                {
                    "station_id": row["station_id"],
                    "name": row["station_name"],
                    "type": None,
                    "network": None,
                    "layers": set(),
                    "clip_seconds": 0.0,
                },
            )
            u["layers"].add("hail")
    for u in used.values():
        u["layers"] = sorted(u["layers"])
        u["clip_seconds"] = round(u["clip_seconds"], 2)
    rad = info["rain"].get("radar_drivers", [])
    if rad:
        used["radar_volumes"] = {
            "station_id": "radar_volumes",
            "name": "merged radar volumes (gridded radars, reflectivity)",
            "type": "radar",
            "network": None,
            "credit": next(
                (
                    s.get("credit")
                    for s in (b.manifest.get("sources") or [])
                    if str(s.get("name", "")).lower().startswith("radar")
                ),
                None,
            ),
            "layers": ["rain"],
            "clip_seconds": round(sum(s["clip_s"][1] - s["clip_s"][0] for s in rad), 2),
        }
    n_src = int(sum(r["n_sources"] for r in rows if r["status"] == "rendered"))
    return {
        "label": LABEL,
        "thunder": {
            "instrument": "lightning mapping array (LMA) VHF sources",
            "flashes_total": len(rows),
            "flashes_rendered": sum(r["status"] == "rendered" for r in rows),
            "sources_in_rendered_flashes": n_src,
        },
        "rain": drivers["rain"],
        "rain_from_radar": rad,
        "wind": drivers["wind"],
        "wind_events": {
            "gust_fronts": info["wind"].get("gust_fronts", []),
            "pressure_jumps": info["wind"].get("pressure_jumps", []),
        },
        "hail": info["hail"].get("rows", []),
        "tornado": {
            "evidence": info["tornado"].get("evidence", []),
            "tracks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "ef": t.ef,
                    "data_s": [float(t.t[0]), float(t.t[-1])],
                    "source": t.meta.get("source") or t.meta.get("survey"),
                    "path_kind": t.meta.get("path_kind"),
                    "credit": t.meta.get("credit"),
                }
                for t in b.tornadoes
                if f"track:{t.id}" in info["tornado"].get("evidence", [])
            ],
            "stylised": True,
        },
        "convective": {"source": info["convective"].get("source")},
        "stations_used": sorted(used.values(), key=lambda u: -u["clip_seconds"]),
        "manifest_sources": b.manifest.get("sources"),
    }


def _per_second_db(x: np.ndarray, sr: int) -> np.ndarray:
    e = level_env_db(x, sr)
    from .scene import CTL_RATE

    m = len(e) // CTL_RATE
    if m == 0:
        return e
    return 10 * np.log10(
        np.maximum((10 ** (e[: m * CTL_RATE] / 10)).reshape(m, CTL_RATE).mean(axis=1), 1e-14)
    )


def render_audio(
    bundle_dir,
    timeline,
    camera_path,
    out_wav,
    *,
    report_path=None,
    events=None,
    sample_rate: int = 48000,
    bits: int = 24,
    seed: int = 1,
    target_lufs: float = -23.0,
    thunder: ThunderParams | None = None,
    stems_dir=None,
    max_listener_height_m: float | None = 500.0,
    thunder_peak_dbfs: float | None = None,
) -> dict:
    """Render the soundtrack to a 48 kHz stereo WAV (16 or 24 bit) aligned to the clip, and write
    ``audio_report.json`` next to it (or at ``report_path``). Returns the report.

    ``timeline`` / ``camera_path`` / ``events``: a path, the parsed JSON, or the objects.
    Deterministic: the same inputs and seed give the identical file.
    """
    y, report, stems = render_mix(
        bundle_dir,
        timeline,
        camera_path,
        events=events,
        sample_rate=sample_rate,
        seed=seed,
        target_lufs=target_lufs,
        thunder=thunder,
        max_listener_height_m=max_listener_height_m,
        thunder_peak_dbfs=thunder_peak_dbfs,
    )
    out = Path(out_wav)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_wav(out, y.T, sample_rate, bits, seed)
    report["output"] = {
        "wav": str(out),
        "bits": bits,
        "channels": 2,
        "sample_rate": sample_rate,
        "duration_s": y.shape[1] / sample_rate,
    }
    rp = Path(report_path) if report_path else out.with_name("audio_report.json")
    rp.write_text(json.dumps(report, indent=1))
    if stems_dir:
        sd = Path(stems_dir)
        sd.mkdir(parents=True, exist_ok=True)
        for k, s in stems.items():
            write_wav(sd / f"stem_{k}.wav", s.T, sample_rate, 24, seed)
    return report


__all__ = ["Timeline", "render_audio", "render_mix"]

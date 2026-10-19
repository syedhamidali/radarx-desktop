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
from .layers import (
    render_ambient,
    render_convective,
    render_hail,
    render_rain,
    render_tornado,
    render_wind,
)
from .mix import PRIORITY, level_env_db, lufs, master, write_wav
from .scene import Scene
from .thunder import ThunderParams, add_at, plan_flash, render_flash, select_flashes
from .timeline import Timeline, load_camera, load_timeline

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
):
    """Render in memory. Returns (stereo (2, N) float array, report dict, stems dict)."""
    sr = int(sample_rate)
    tl_src = timeline
    b = bundle if isinstance(bundle, Bundle) else load_bundle(bundle)
    tl = load_timeline(timeline, b.window_start)
    cam = load_camera(camera_path)
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
    counts = select_flashes(rows, prm)
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

    y, minfo, stems = master({k: raw[k] for k in LAYER_NAMES}, sr, target_lufs)

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
    shown = [_public(r) for r in rows]
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
            "sample_rate": sr, "seed": seed, "target_lufs": target_lufs,
            "speed_of_sound_ms": SPEED_OF_SOUND_MS,
            "thunder": asdict(prm),
            "clip_duration_s": dur,
            "speedup": tl.speedup if tl.speedup is not None else "keyframes",
            "data_offset_s": tl.data_offset_s,
        },
        "master": minfo,
        "layers": per_layer,
        "thunder": {
            "counts": counts,
            "rendered": counts.get("rendered", 0),
            "skipped": {k: v for k, v in counts.items() if k != "rendered"},
            "flashes": shown,
        },
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


def _per_second_db(x: np.ndarray, sr: int) -> np.ndarray:
    e = level_env_db(x, sr)
    from .scene import CTL_RATE

    m = len(e) // CTL_RATE
    if m == 0:
        return e
    return 10 * np.log10(np.maximum((10 ** (e[: m * CTL_RATE] / 10)).reshape(m, CTL_RATE).mean(axis=1), 1e-14))


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
) -> dict:
    """Render the soundtrack to a 48 kHz stereo WAV (16 or 24 bit) aligned to the clip, and write
    ``audio_report.json`` next to it (or at ``report_path``). Returns the report.

    ``timeline`` / ``camera_path`` / ``events``: a path, the parsed JSON, or the objects.
    Deterministic: the same inputs and seed give the identical file.
    """
    y, report, stems = render_mix(
        bundle_dir, timeline, camera_path, events=events, sample_rate=sample_rate, seed=seed,
        target_lufs=target_lufs, thunder=thunder,
    )
    out = Path(out_wav)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_wav(out, y.T, sample_rate, bits, seed)
    report["output"] = {"wav": str(out), "bits": bits, "channels": 2, "sample_rate": sample_rate,
                        "duration_s": y.shape[1] / sample_rate}
    rp = Path(report_path) if report_path else out.with_name("audio_report.json")
    rp.write_text(json.dumps(report, indent=1))
    if stems_dir:
        sd = Path(stems_dir)
        sd.mkdir(parents=True, exist_ok=True)
        for k, s in stems.items():
            write_wav(sd / f"stem_{k}.wav", s.T, sample_rate, 24, seed)
    return report


__all__ = ["Timeline", "render_audio", "render_mix"]

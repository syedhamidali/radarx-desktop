"""Layouts of the tornado focus (tools/perils_movie/render/focus.py): the film's 120 s and the stand-alone clip.

``plan_focus(perils, "film")`` gives the plan of perils_qlcs_movie_v3.mp4 (establishing and flyby, the five angles, the
ground-level view, the rise and the top-down finale); ``plan_focus(perils, "clip")`` the plan of perils_tornado_focus.mp4
(the five angles only, about 75 s). Both return the dict of ``choreography.plan``: timeline, camera, events, shots.
"""

from __future__ import annotations

import math
from datetime import timedelta

import choreography as ch
import numpy as np
from choreography import Scene, Shot, bearing_deg
from convert_bundle import parse_iso
from focus import Rig, Seg, build_timeline_segments, layout_shots

REAL_TOUCH_S, REAL_WINDOW_S = (
    240.0,
    960.0,
)  # touchdown and window end of the case the anchors below are written for

# (name, clip seconds, kind, data seconds after the window start) of the film; the case's own numbers.
# kind: "rt" = one data second per clip second; "lapse" = speeds up between two real-time stretches; "cruise" = constant.
FILM_SEGS = [
    ("establishing", 9, "cruise", 0.0),
    ("flyby", 12, "cruise", 70.0),
    ("focus_wide", 5, "cruise", 180.0),
    ("focus_chaser", 18, "rt", 237.0),  # touchdown at 240: 3 s before it, then the funnel forms
    ("lapse1", 3, "lapse", 255.0),
    ("focus_close", 8, "rt", 300.0),
    ("lapse2", 3, "lapse", 308.0),
    ("focus_low", 7, "rt", 400.0),
    ("focus_swath", 6, "rt", 407.0),
    ("focus_top", 6, "cruise", 413.0),
    ("window", 8, "cruise", 520.0),
    ("rise", 6, "cruise", 620.0),
    ("topdown", 29, "cruise", 680.0),
]
CLIP_SEGS = [
    ("focus_wide", 8, "cruise", 150.0),
    ("focus_chaser", 18, "rt", 237.0),
    ("lapse1", 4, "lapse", 255.0),
    ("focus_close", 12, "rt", 300.0),
    ("lapse2", 4, "lapse", 312.0),
    ("focus_low", 10, "rt", 400.0),
    ("focus_swath", 10, "rt", 410.0),
    ("lapse3", 3, "lapse", 420.0),
    ("focus_top", 14, "cruise", 500.0),
    ("end", 3, "rt", 957.0),
]
FINALE_ALT_KM = (
    26.0,
    34.0,
)  # height of the top-down finale: the track (17.7 km) fills a third of the frame


def anchor_map(window_s: float, t_touch: float):
    """Map the case's anchors (touchdown 240 s, window 960 s) to another case: the identity for it, a proportional squeeze
    before and after the touchdown for a shorter one (the synthetic test bundles)."""

    def f(x: float) -> float:
        if x <= REAL_TOUCH_S:
            return x * t_touch / REAL_TOUCH_S
        return t_touch + (x - REAL_TOUCH_S) * (window_s - t_touch) / (REAL_WINDOW_S - REAL_TOUCH_S)

    return f


def make_segments(spec, window_s: float, t_touch: float) -> list[Seg]:
    f = anchor_map(window_s, t_touch)
    segs = [Seg(n, float(L), k, f(td)) for n, L, k, td in spec]
    # a real-time stretch that does not fit before the next anchor, or a lapse that would have to go slower than the clock (a short
    # case), becomes a cruise at the speed its data span asks for
    for j, s in enumerate(segs):
        nxt = segs[j + 1].td0 if j + 1 < len(segs) else window_s
        if (s.kind == "rt" and s.td0 + s.length > nxt + 1e-6) or (
            s.kind == "lapse" and nxt - s.td0 < 1.5 * s.length
        ):
            s.kind = "cruise"
    return segs


def edges_of(segs: list[Seg]) -> dict[str, tuple[float, float]]:
    t, out = 0.0, {}
    for s in segs:
        out[s.name] = (t, t + s.length)
        t += s.length
    return out


def beat_report(rig: Rig, tl: dict, camera: list[dict], segs: list[Seg]) -> list[dict]:
    """What the audience sees in every stretch: clip and data times, speed-up, camera distance / bearing / height / lens."""
    ed = edges_of(segs)
    out = []
    for s in segs:
        a, b = ed[s.name]
        tm = 0.5 * (a + b)
        i = min(len(camera) - 1, round(tm * tl["fps"]))
        c = camera[i]
        q = rig.T(tm)[0]
        dx, dy = c["x_km"] - q[0], c["y_km"] - q[1]
        td0, td1 = float(rig.td(a)), float(rig.td(b))
        out.append(
            {
                "name": s.name,
                "kind": s.kind,
                "t0": round(a, 3),
                "t1": round(b, 3),
                "data_t0_s": round(td0, 2),
                "data_t1_s": round(td1, 2),
                "speedup_mean": round((td1 - td0) / max(b - a, 1e-9), 2),
                "tornado_distance_m": round(1000.0 * math.hypot(dx, dy)),
                "camera_bearing_from_tornado_deg": round(float(bearing_deg(dx, dy)), 1),
                "camera_height_m": round(c["alt_m"], 1),
                "heading_deg": round(c["heading_deg"], 1),
                "pitch_deg": round(c["pitch_deg"], 1),
                "fov_deg": round(c["fov_deg"], 1),
            }
        )
    return out


def sim_span(rig: Rig, ed: dict) -> list[float]:
    """Data seconds [start, end] the tornado simulation runs over: from just before the chaser's start to the end of the last
    angle that can show it (the oblique last angle sees the trees down); it fades out when the camera is far from the tornado."""
    a = ed["focus_chaser"][0] - 1.0
    b = ed["focus_swath"][1]
    return [round(float(rig.td(a)), 2), round(float(rig.td(b)), 2)]


def plan_focus(perils: dict, layout: str = "film", fps: int = 30) -> dict:
    """The film's plan (``layout="film"``, 120 s) or the stand-alone clip's (``"clip"``, 76 s), as ``choreography.plan()``'s dict."""
    scene = Scene(perils)
    span = scene.tornado_span()
    if span is None:
        raise ValueError("the tornado focus needs a surveyed tornado inside the window")
    window = scene.window_s
    segs = make_segments(FILM_SEGS if layout == "film" else CLIP_SEGS, window, span[0])
    tl = build_timeline_segments(segs, window, fps)
    duration = float(sum(s.length for s in segs))
    rig = Rig(scene, tl)
    foc = layout_shots(rig, segs, edges_of(segs), end_top=(layout == "clip"))
    ed = edges_of(segs)
    station = None
    if layout == "film":
        station = ch.pick_station(scene, tl, ed["window"][0], ed["window"][1])
        lay = {
            "est_end": ed["establishing"][1],
            "fly_end": ed["flyby"][1],
            "win_s": ed["window"][1] - ed["window"][0],
            "rise_s": ed["rise"][1] - ed["rise"][0],
            "top_s": ed["topdown"][1] - ed["topdown"][0],
            "top": (tuple(float(v) for v in rig.mid), FINALE_ALT_KM),
        }
        shots: list[Shot] = ch.plan_shots(
            scene, tl, duration, ed["flyby"][1], station, focus=foc, layout=lay
        )
    else:
        shots = foc
    camera = ch.build_camera(scene, tl, shots)
    events = ch.build_events(scene, tl, camera, shots, station)
    beats = beat_report(rig, tl, camera, segs)
    events["focus"] = {"layout": layout, "beats": beats}
    t0 = parse_iso(perils["time0"])

    def iso(s):
        return (t0 + timedelta(seconds=float(s))).isoformat().replace("+00:00", "Z")

    sp = tl["speedup"]
    f0, f1 = float(tl["t_data_s"][0]), float(tl["t_data_s"][-1])
    timeline = {
        "data_start": iso(0.0),
        "data_end": iso(window),
        "shown_from": iso(f0),
        "shown_to": iso(f1),
        "data_window_s": window,
        "clip_duration_s": duration,
        "fps": fps,
        "frames": tl["frames"],
        "speedup_mean": (f1 - f0) / duration,
        "speedup_min": float(np.min(sp)),
        "speedup_max": float(np.max(sp)),
        "note": "t_data_s[i] = seconds after data_start shown at clip time i / fps (monotone); speedup[i] = data seconds per clip second",
        "tornado_slowdown": None,
        "end_card_s": 9.0 if layout == "film" else 3.5,
        "focus": {"layout": layout, "sim_span_data_s": sim_span(rig, ed), "beats": beats},
        "mapping": {
            "t_clip_s": [round(float(v), 4) for v in tl["t_clip_s"]],
            "t_data_s": [round(float(v), 4) for v in tl["t_data_s"]],
        },
        "speedup": [round(float(v), 4) for v in sp],
        "shots": events["shots"],
    }
    return {
        "timeline": timeline,
        "camera": camera,
        "events": events,
        "station": station and station["id"],
        "tl": tl,
        "shots": shots,
        "segments": segs,
    }


def write_focus(perils_path, out_dir, layout: str = "film", fps: int = 30, log=print) -> dict:
    """Plan and write timeline.json, camera_path.json and events.json (the files the page and the sound read)."""
    import json
    from pathlib import Path

    perils = json.loads(Path(perils_path).read_text())
    res = plan_focus(perils, layout, fps)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "timeline.json").write_text(json.dumps(res["timeline"]))
    (out / "camera_path.json").write_text(json.dumps(res["camera"]))
    (out / "events.json").write_text(json.dumps(res["events"]))
    tm = res["timeline"]
    log(
        f"tornado focus ({layout}): {tm['clip_duration_s']:g} s, data {tm['shown_from'][11:19]} - {tm['shown_to'][11:19]} UTC, "
        f"speed-up x{tm['speedup_min']:.1f}..x{tm['speedup_max']:.1f}; ground-level station {res['station']}"
    )
    for b in tm["focus"]["beats"]:
        log(
            f"  {b['t0']:6.1f}-{b['t1']:6.1f} s  {b['name']:13s} x{b['speedup_mean']:5.1f}  {b['tornado_distance_m']:6d} m  bearing {b['camera_bearing_from_tornado_deg']:5.1f}  {b['camera_height_m']:8.1f} m high  lens {b['fov_deg']:.0f}"
        )
    return res

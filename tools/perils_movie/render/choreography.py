"""The choreography of the PERiLS movie: the data timeline, the camera path and the event list.

Python is the single authority: the clip page only reads the files this module writes
(``timeline.json``, ``camera_path.json``, ``events.json``), and the sound agent syncs to the same files.

* **Timeline** (clip time -> data time): monotone, never backwards, built from a speed profile that
  is 1 everywhere except for a slow-down (default 0.35 of the base speed) around the tornado shot, then
  normalised so that the whole data window fits the clip. The speed-up is printed on screen.
* **Camera** (one entry per frame): shots expressed in the frame of the line (centre, motion direction,
  along-line direction at the data time of the frame), so the camera rides with the moving storm:
  establishing wide oblique -> line-parallel low flyby -> tornado shot (a safe distance ahead of the
  tornado, the line behind it, slow orbit) -> ground-level window camera at a disdrometer station
  looking at the approaching line -> rise behind the line -> top-down finale. Each shot gives the eye
  position P(t) and the point looked at T(t); consecutive shots are blended with a smoothstep over a few
  seconds, so the path and its first derivative are continuous (no jumps). Heading and pitch follow
  from P and T.
* **Events**: flashes (clip time of the first LMA source and position), tornado positions in clip
  time, hail candidates, the station nearest the camera through the clip, shot list.

Pure numpy; unit-tested in python/tests/test_perils_movie.py.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from convert_bundle import Geo, parse_iso

EYE_M = 2.0  # eye height of the ground-level (window) camera above the ground, m


# ── basic maths ─────────────────────────────────────────────────────────────


def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def bearing_deg(dx, dy):
    return (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0


def unit(bearing):
    b = np.radians(bearing)
    return np.array([np.sin(b), np.cos(b)])


def circular_mean_deg(a):
    a = np.radians(np.asarray(a, float))
    return float(np.degrees(np.arctan2(np.sin(a).mean(), np.cos(a).mean())) % 360.0)


# ── timeline ────────────────────────────────────────────────────────────────


def speed_profile(
    t: np.ndarray,
    dip_centre: float | None,
    dip_width: float = 24.0,
    dip_level: float = 0.35,
    edge: float = 4.0,
) -> np.ndarray:
    """Relative speed of the data clock at clip times ``t``: 1, and ``dip_level`` around ``dip_centre`` (smooth edges)."""
    s = np.ones_like(t, dtype=float)
    if dip_centre is None:
        return s
    h = dip_width / 2.0
    ramp = smoothstep((h + edge - np.abs(t - dip_centre)) / edge)
    return 1.0 - (1.0 - dip_level) * ramp


def build_timeline(
    window_s: float,
    duration: float = 120.0,
    fps: int = 30,
    dip_centre: float | None = None,
    dip_width: float = 24.0,
    dip_level: float = 0.35,
) -> dict:
    """Dense mapping: ``t_data_s[i]`` = seconds after the data start shown at frame i (0..N, N = duration * fps)."""
    n = round(duration * fps)
    t = np.arange(n + 1) / fps
    sp = speed_profile(t, dip_centre, dip_width, dip_level)
    cum = np.concatenate([[0.0], np.cumsum((sp[1:] + sp[:-1]) * 0.5 / fps)])
    td = cum / cum[-1] * window_s
    rate = np.gradient(td, t)  # data seconds per clip second
    return {"fps": fps, "frames": n, "t_clip_s": t, "t_data_s": td, "speedup": rate}


def clip_time_of_data(tl: dict, td):
    """Inverse of the mapping (monotone): clip time at which data time ``td`` is shown."""
    return np.interp(td, tl["t_data_s"], tl["t_clip_s"])


def data_time_of_clip(tl: dict, t):
    return np.interp(t, tl["t_clip_s"], tl["t_data_s"])


def solve_dip_centre(
    window_s: float,
    target_td: float,
    duration: float,
    fps: int,
    lo: float,
    hi: float,
    dip_width: float = 24.0,
    dip_level: float = 0.35,
) -> float:
    """Clip time ``c`` in [lo, hi] such that, with the slow-down centred on ``c``, data time ``target_td`` is shown at ``c``."""

    def f(c: float) -> float:
        tl = build_timeline(window_s, duration, fps, c, dip_width, dip_level)
        return float(data_time_of_clip(tl, c)) - target_td

    if f(lo) >= 0:
        return lo
    if f(hi) <= 0:
        return hi
    a, b = lo, hi
    for _ in range(40):
        m = 0.5 * (a + b)
        if f(m) < 0:
            a = m
        else:
            b = m
    return 0.5 * (a + b)


# ── the scene seen from the data ────────────────────────────────────────────


class Scene:
    """Line, tornado, stations and radars of a converted bundle (perils.json), in km of the scene frame."""

    def __init__(self, perils: dict):
        self.p = perils
        self.geo = Geo(perils["origin"]["lat"], perils["origin"]["lon"])
        tr = perils["track"]
        self.tt = np.array([q["t"] for q in tr], float)
        self.tx = np.array([q["x"] for q in tr], float)
        self.ty = np.array([q["y"] for q in tr], float)
        lines = [q for q in perils.get("line", []) if q]
        self.perp_deg = circular_mean_deg([q["perp_deg"] for q in lines]) if lines else 75.0
        self.front_km = float(np.mean([q["front_km"] for q in lines])) if lines else 5.0
        self.back_km = float(np.mean([q["back_km"] for q in lines])) if lines else -20.0
        self.half_len_km = float(np.mean([q["half_len_km"] for q in lines])) if lines else 60.0
        self.window_s = float(perils["steps"][-1]["t_s"] - perils["steps"][0]["t_s"])
        self.m = unit(self.perp_deg)  # motion direction (unit, x east / y north)
        self.a = np.array([-self.m[1], self.m[0]])  # along the line, to the left of the motion

    def centre(self, td):
        """Line centre (km) at data time(s) ``td``; beyond the steps it moves on at the end velocity."""
        td = np.asarray(td, float)
        x = np.interp(td, self.tt, self.tx)
        y = np.interp(td, self.tt, self.ty)
        if len(self.tt) > 1:
            vx = (self.tx[-1] - self.tx[-2]) / (self.tt[-1] - self.tt[-2])
            vy = (self.ty[-1] - self.ty[-2]) / (self.tt[-1] - self.tt[-2])
            over = np.clip(td - self.tt[-1], 0, None)
            x = x + vx * over
            y = y + vy * over
        return np.stack([x, y], -1)

    def tornado_at(self, td):
        """(x, y, active) of the first tornado with a path at data time(s) ``td``: clamped to its ends, active inside them."""
        td = np.asarray(td, float)
        tors = [t for t in self.p.get("tornadoes", []) if len(t.get("path", [])) >= 2]
        if not tors:
            return None
        path = tors[0]["path"]
        pt = np.array([q["t"] for q in path])
        x = np.interp(td, pt, [q["x"] for q in path])
        y = np.interp(td, pt, [q["y"] for q in path])
        return np.stack([x, y], -1), (td >= pt[0]) & (td <= pt[-1])

    def tornado_span(self):
        tors = [t for t in self.p.get("tornadoes", []) if len(t.get("path", [])) >= 2]
        if not tors:
            return None
        pt = [q["t"] for q in tors[0]["path"]]
        return min(pt), max(pt)

    def station_arrival(self, st: dict) -> tuple[float, float] | None:
        """(time of the series' rain-rate maximum, that rate) of a station, or None without a series."""
        s = st.get("series") or {}
        t, r = s.get("t_s"), s.get("rain_rate_mmh")
        if not t or not r:
            return None
        arr = np.array([np.nan if v is None else v for v in r], float)
        if not np.isfinite(arr).any():
            return None
        i = int(np.nanargmax(arr))
        return float(t[i]), float(arr[i])


# ── shots ───────────────────────────────────────────────────────────────────


def _pad(v, n):
    return np.broadcast_to(np.asarray(v, float), (n,)).copy()


class Shot:
    def __init__(self, name: str, label: str, t0: float, t1: float, fn, vexag: float):
        self.name, self.label, self.t0, self.t1, self.fn, self.vexag = (
            name,
            label,
            t0,
            t1,
            fn,
            vexag,
        )


def plan_shots(
    scene: Scene, tl: dict, duration: float, tor_c: float | None, station: dict | None
) -> list[Shot]:
    """The shot list. Times are clip seconds; ``tor_c`` is the clip time of the tornado shot's middle (or None)."""
    m, a = scene.m, scene.a
    front = scene.front_km
    L = scene.half_len_km

    def cen(t):
        return scene.centre(data_time_of_clip(tl, t))

    def col(v):
        return np.asarray(v, float)[:, None]

    def estab(t):
        u = t / 17.0
        R = 150.0 - 35.0 * u
        th = np.radians(32.0 - 22.0 * u)
        # ahead of the line and a little to its right (south side): the line comes toward the viewer
        P = cen(t) + col(R) * (col(np.cos(th)) * m[None, :] - col(np.sin(th)) * a[None, :])
        return P, _pad(58.0 - 24.0 * u, len(t)), cen(t), _pad(3.0, len(t))

    b0 = 15.0
    b1 = (tor_c - 12.0) if tor_c is not None else 40.0

    def fly(t):
        u = np.clip((t - b0) / max(1e-6, b1 - b0), 0, 1)
        s = (
            -0.62 + 1.24 * smoothstep(u)
        ) * L  # along-line position (km): from the south end to the north end
        c = cen(t)
        d = front + 24.0 + 4.0 * np.sin(u * math.pi)
        P = c + col(s) * a[None, :] + col(d) * m[None, :]
        look = c + col(s + 0.22 * L) * a[None, :] + (front - 6.0) * m[None, :]
        return P, 2.2 + 1.3 * np.sin(u * math.pi), look, _pad(3.2, len(t))

    shots = [
        Shot("establishing", "Establishing view of the whole line", 0.0, b0, estab, 2.4),
        Shot("flyby", "Low flyby along the line", b0, b1, fly, 2.4),
    ]
    t_after = b1
    if tor_c is not None:

        def tor(t):
            q, _act = scene.tornado_at(data_time_of_clip(tl, t))
            u = np.clip((t - (tor_c - 12.0)) / 24.0, 0, 1)
            # a safe distance AHEAD of the tornado (the line is behind it), orbiting slowly
            ang = np.radians(scene.perp_deg) + np.radians(-35.0 + 70.0 * smoothstep(u))
            dist = 9.0 - 2.0 * np.sin(u * math.pi)
            P = q + col(dist) * np.stack([np.sin(ang), np.cos(ang)], -1)
            return P, 0.55 + 0.9 * smoothstep(u), q, _pad(0.8, len(t))

        shots.append(
            Shot(
                "tornado",
                "Tornado: a safe distance ahead, the line behind",
                tor_c - 12.0,
                tor_c + 12.0,
                tor,
                1.8,
            )
        )
        t_after = tor_c + 12.0
    t_st1 = min(duration - 30.0, t_after + 27.0)
    if station is not None:
        sx, sy = station["x"], station["y"]
        look_b = (scene.perp_deg + 180.0) % 360.0  # looking back toward the approaching line

        def stat(t):
            u = np.clip((t - t_after) / max(1e-6, t_st1 - t_after), 0, 1)
            # a slow pan while the line approaches, then (after it has passed) a turn to watch it leave
            yaw = np.radians(
                look_b
                + 14.0 * (1.0 - 2.0 * smoothstep(u / 0.7))
                + 180.0 * smoothstep((u - 0.7) / 0.3)
            )
            P = np.stack([np.full(len(t), sx), np.full(len(t), sy)], -1)
            T = P + 30.0 * np.stack([np.sin(yaw), np.cos(yaw)], -1)
            return (
                P,
                _pad(EYE_M / 1000.0, len(t)),
                T,
                _pad(EYE_M / 1000.0 + 30.0 * math.tan(math.radians(7.0)), len(t)),
            )

        shots.append(
            Shot(
                "window",
                f"Ground-level view at {station.get('name', station['id'])}",
                t_after,
                t_st1,
                stat,
                1.0,
            )
        )
    else:
        t_st1 = t_after
    e1 = duration - 15.0

    def rise(t):
        u = smoothstep((t - t_st1) / max(1e-6, e1 - t_st1))
        P = cen(t) - col(20.0 + 45.0 * u) * m[None, :] + col(10.0 * u) * a[None, :]
        return P, 2.0 + 28.0 * u, cen(t) + 8.0 * m, _pad(3.0, len(t))

    shots.append(Shot("rise", "Behind the line, rising", t_st1, e1, rise, 2.4))

    def top(t):
        u = np.clip((t - e1) / (duration - e1), 0, 1)
        c = cen(t)
        # looking down with the line's motion toward the top of the frame (a small offset along the
        # motion fixes the heading, and it is the heading the rising shot arrives with: no spin)
        return c, 62.0 + 16.0 * smoothstep(u), c + 7.0 * m, _pad(0.0, len(t))

    shots.append(Shot("topdown", "Top-down: the line from above", e1, duration, top, 3.0))
    return shots


def blend_shots(shots: list[Shot], t: np.ndarray, overlap: float = 5.0):
    """P (km, scene), eye altitude (km), look-at point and its height, vexag, shot index per frame.

    Shots crossfade in sequence: shot j fades in over ``overlap`` seconds centred on its start
    (smoothstep weight), on top of the blend of the shots before it. Every shot is continuous with a
    continuous first derivative, and so is each weight, so the result is too.
    """
    t = np.asarray(t, float)
    n = len(t)
    first = shots[0].fn(np.clip(t, shots[0].t0 - overlap, shots[0].t1 + overlap))
    P = first[0].astype(float).copy()
    alt = np.asarray(first[1], float).copy()
    T = first[2].astype(float).copy()
    tz = np.asarray(first[3], float).copy()
    vex = np.full(n, shots[0].vexag, float)
    idx = np.zeros(n, int)
    for j, s in enumerate(shots[1:], start=1):
        w = smoothstep((t - (s.t0 - overlap / 2.0)) / overlap)
        ev = s.fn(np.clip(t, s.t0 - overlap, s.t1 + overlap))
        P = (1 - w)[:, None] * P + w[:, None] * ev[0]
        alt = (1 - w) * alt + w * np.asarray(ev[1], float)
        T = (1 - w)[:, None] * T + w[:, None] * ev[2]
        tz = (1 - w) * tz + w * np.asarray(ev[3], float)
        vex = (1 - w) * vex + w * s.vexag
        idx = np.where(w >= 0.5, j, idx)
    return P, alt, T, tz, vex, idx


def build_camera(scene: Scene, tl: dict, shots: list[Shot]) -> list[dict]:
    t = tl["t_clip_s"]
    P, alt, T, tz, vex, idx = blend_shots(shots, t)
    d = T - P
    horiz = np.hypot(d[:, 0], d[:, 1])
    heading = bearing_deg(d[:, 0], d[:, 1])
    # the renderer stretches heights by the vertical exaggeration of the frame: so does the pitch
    pitch = np.degrees(np.arctan2((tz - alt) * vex, np.maximum(horiz, 1e-6)))
    pitch = np.clip(pitch, -86.0, 86.0)
    lat, lon = scene.geo.latlon(P[:, 0], P[:, 1])
    out = []
    for i in range(len(t)):
        out.append(
            {
                "t_clip_s": round(float(t[i]), 4),
                "lat": round(float(lat[i]), 6),
                "lon": round(float(lon[i]), 6),
                "alt_m": round(float(alt[i]) * 1000.0, 1),
                "heading_deg": round(float(heading[i]), 3),
                "pitch_deg": round(float(pitch[i]), 3),
                "x_km": round(float(P[i, 0]), 4),
                "y_km": round(float(P[i, 1]), 4),
                "z_km": round(float(alt[i]), 5),
                "vexag": round(float(vex[i]), 4),
                "shot": shots[min(idx[i], len(shots) - 1)].name,
            }
        )
    return out


# ── nearest station, events ─────────────────────────────────────────────────


def nearest_station(stations: list[dict], x: float, y: float, z_km: float) -> str | None:
    """Id of the station nearest to the eye (3-D distance in km, stations on the ground)."""
    best, bd = None, 1e18
    for s in stations:
        d = math.sqrt((s["x"] - x) ** 2 + (s["y"] - y) ** 2 + (z_km - 0.0) ** 2)
        if d < bd:
            best, bd = s["id"], d
    return best


def build_events(
    scene: Scene, tl: dict, camera: list[dict], shots: list[Shot], station: dict | None
) -> dict:
    p = scene.p
    geo = scene.geo
    ev: dict = {
        "flashes": [],
        "tornado": None,
        "hail": [],
        "nearest_station": [],
        "shots": [],
        "observer_station": None,
    }
    for f in (p.get("lma") or {}).get("flashes", []):
        s = np.asarray(f["s"], float)
        if not len(s):
            continue
        tc = float(clip_time_of_data(tl, f["t0"]))
        if not (0.0 <= tc <= tl["t_clip_s"][-1]):
            continue
        lat, lon = geo.latlon(s[:, 1].mean(), s[:, 2].mean())
        ev["flashes"].append(
            {
                "id": f["id"],
                "t_clip_s": round(tc, 3),
                "t_data_s": round(f["t0"], 3),
                "n_sources": int(f.get("n", len(s))),
                "lat": round(float(lat), 5),
                "lon": round(float(lon), 5),
                "alt_m": round(float(s[:, 3].mean()) * 1000.0, 0),
            }
        )
    span = scene.tornado_span()
    if span:
        tors = [t for t in p["tornadoes"] if len(t.get("path", [])) >= 2]
        t_a, t_b = float(clip_time_of_data(tl, span[0])), float(clip_time_of_data(tl, span[1]))
        pos = []
        for tc in np.arange(math.ceil(t_a), t_b + 1e-9, 1.0):
            q, _act = scene.tornado_at(float(data_time_of_clip(tl, tc)))
            la, lo = geo.latlon(q[0], q[1])
            pos.append(
                {"t_clip_s": float(tc), "lat": round(float(la), 5), "lon": round(float(lo), 5)}
            )
        ev["tornado"] = {
            "id": tors[0].get("id"),
            "rating": tors[0].get("rating"),
            "t_clip_start_s": round(t_a, 3),
            "t_clip_end_s": round(t_b, 3),
            "positions": pos,
        }
    for s in p.get("stations", []):
        for h in s.get("events") or []:
            t = h.get("t_s")
            if t is None:
                continue
            ev["hail"].append(
                {
                    "station": s["id"],
                    "t_clip_s": round(float(clip_time_of_data(tl, t)), 3),
                    "t_data_s": t,
                    "lat": s["lat"],
                    "lon": s["lon"],
                    **{k: v for k, v in h.items() if k not in ("t_s",)},
                }
            )
    last = None
    for c in camera:
        sid = nearest_station(p.get("stations", []), c["x_km"], c["y_km"], c["z_km"])
        if sid != last:
            ev["nearest_station"].append({"t_clip_s": c["t_clip_s"], "id": sid})
            last = sid
    ev["shots"] = [
        {"name": s.name, "label": s.label, "t0": round(s.t0, 3), "t1": round(s.t1, 3)}
        for s in shots
    ]
    if station:
        ev["observer_station"] = {
            "id": station["id"],
            "name": station.get("name"),
            "lat": station["lat"],
            "lon": station["lon"],
            "eye_m": EYE_M,
        }
    return ev


def pick_station(scene: Scene, tl: dict, t0: float, t1: float) -> dict | None:
    """The station for the ground-level shot [t0, t1]: the strongest rain-rate maximum that arrives inside the shot, else the nearest in time."""
    sts = scene.p.get("stations", [])
    best, score = None, -1e18
    ta, tb = float(data_time_of_clip(tl, t0)), float(data_time_of_clip(tl, t1))
    for s in sts:
        arr = scene.station_arrival(s)
        if arr is None:
            continue
        tm, rr = arr
        inside = ta + 0.25 * (tb - ta) <= tm <= tb - 0.15 * (tb - ta)
        sc = (1e6 if inside else -abs(tm - 0.6 * (ta + tb) - 0.4 * ta)) + (rr if inside else 0.0)
        if sc > score:
            best, score = s, sc
    if best is None and sts:
        best = sts[0]
    return best


def plan(
    perils: dict,
    duration: float = 120.0,
    fps: int = 30,
    dip_level: float = 0.35,
    dip_width: float = 24.0,
) -> dict:
    """Everything: timeline.json, camera_path.json and events.json contents."""
    scene = Scene(perils)
    window = scene.window_s
    span = scene.tornado_span()
    tor_c = None
    if span:
        mid = 0.5 * (span[0] + span[1])
        tor_c = solve_dip_centre(
            window, mid, duration, fps, 36.0, duration - 62.0, dip_width, dip_level
        )
    tl = build_timeline(window, duration, fps, tor_c, dip_width, dip_level)
    # the ground-level shot follows the tornado shot (or the flyby); pick its station from its time slot
    t_after = (tor_c + dip_width / 2.0) if tor_c is not None else 40.0
    station = pick_station(scene, tl, t_after, min(duration - 30.0, t_after + 27.0))
    shots = plan_shots(scene, tl, duration, tor_c, station)
    camera = build_camera(scene, tl, shots)
    events = build_events(scene, tl, camera, shots, station)
    t0 = parse_iso(perils["time0"])
    from datetime import timedelta

    iso = lambda s: (t0 + timedelta(seconds=float(s))).isoformat().replace("+00:00", "Z")  # noqa: E731
    sp = tl["speedup"]
    timeline = {
        "data_start": iso(0.0),
        "data_end": iso(window),
        "data_window_s": window,
        "clip_duration_s": duration,
        "fps": fps,
        "frames": tl["frames"],
        "speedup_mean": window / duration,
        "speedup_min": float(sp.min()),
        "speedup_max": float(sp.max()),
        "note": "t_data_s[i] = seconds after data_start shown at clip time i / fps (monotone); speedup[i] = data seconds per clip second",
        "tornado_slowdown": None
        if tor_c is None
        else {"centre_clip_s": tor_c, "width_s": dip_width, "level": dip_level},
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
    }


def write(
    perils_path: Path, out_dir: Path, duration: float = 120.0, fps: int = 30, log=print
) -> dict:
    perils = json.loads(Path(perils_path).read_text())
    res = plan(perils, duration, fps)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "timeline.json").write_text(json.dumps(res["timeline"]))
    (out_dir / "camera_path.json").write_text(json.dumps(res["camera"]))
    (out_dir / "events.json").write_text(json.dumps(res["events"]))
    tm = res["timeline"]
    log(
        f"data window {tm['data_window_s'] / 60:.1f} min in {duration:g} s: mean x{tm['speedup_mean']:.1f}, range x{tm['speedup_min']:.1f}..x{tm['speedup_max']:.1f}; ground-level station {res['station']}"
    )
    for s in res["events"]["shots"]:
        log(f"  {s['t0']:6.1f}-{s['t1']:6.1f} s  {s['label']}")
    return res


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--perils", required=True, help="perils.json written by convert_bundle.py")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--fps", type=int, default=30)
    a = ap.parse_args(argv)
    write(Path(a.perils), Path(a.out_dir), a.duration, a.fps)


if __name__ == "__main__":
    main()

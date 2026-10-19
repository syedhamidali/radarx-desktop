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
from vortex import vortex_params

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
    core_width: float = 0.0,
    core_level: float | None = None,
    core_edge: float = 3.0,
) -> np.ndarray:
    """Relative speed of the data clock at clip times ``t``: 1, and ``dip_level`` around ``dip_centre`` (smooth edges).

    With ``core_level`` the middle ``core_width`` seconds of the slow-down are slower still (``core_level``): the
    stretch in which the tornado is shown in (near) real time.
    """
    s = np.ones_like(t, dtype=float)
    if dip_centre is None:
        return s
    h = dip_width / 2.0
    ramp = smoothstep((h + edge - np.abs(t - dip_centre)) / edge)
    sp = 1.0 - (1.0 - dip_level) * ramp
    if core_level is not None and core_width > 0:
        hc = core_width / 2.0
        rc = smoothstep((hc + core_edge - np.abs(t - dip_centre)) / core_edge)
        sp = sp - (dip_level - core_level) * rc
    return sp


def build_timeline(
    window_s: float,
    duration: float = 120.0,
    fps: int = 30,
    dip_centre: float | None = None,
    dip_width: float = 24.0,
    dip_level: float = 0.35,
    core_speedup: float | None = None,
    core_width: float = 10.0,
) -> dict:
    """Dense mapping: ``t_data_s[i]`` = seconds after the data start shown at frame i (0..N, N = duration * fps).

    ``core_speedup`` (data seconds per clip second, e.g. 1.0 = real time) makes the middle ``core_width`` seconds of
    the slow-down run at that speed-up (solved by a few iterations, because the normalisation depends on it).
    """
    n = round(duration * fps)
    t = np.arange(n + 1) / fps

    def build(core_level):
        sp = speed_profile(t, dip_centre, dip_width, dip_level, 4.0, core_width, core_level)
        cum = np.concatenate([[0.0], np.cumsum((sp[1:] + sp[:-1]) * 0.5 / fps)])
        td = cum / cum[-1] * window_s
        return td, np.gradient(td, t)  # data seconds per clip second

    if dip_centre is not None and core_speedup is not None:
        lvl = min(dip_level, core_speedup / max(window_s / duration, 1e-6))
        for _ in range(30):
            td, rate = build(lvl)
            at = float(np.interp(dip_centre, t, rate))
            lvl = float(np.clip(lvl * core_speedup / max(at, 1e-9), 1e-3, dip_level))
        td, rate = build(lvl)
    else:
        td, rate = build(None)
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
    core_speedup: float | None = None,
    core_width: float = 10.0,
) -> float:
    """Clip time ``c`` in [lo, hi] such that, with the slow-down centred on ``c``, data time ``target_td`` is shown at ``c``."""

    def f(c: float) -> float:
        tl = build_timeline(
            window_s, duration, fps, c, dip_width, dip_level, core_speedup, core_width
        )
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


def smooth_keys(t, keys, width: float = 2.5):
    """Smooth curve through (time, value) keys: linear between them, then a Hann window of ``width`` s, so the
    value and its slope are continuous (a camera that eases through its keys instead of stopping at each)."""
    t = np.asarray(t, float)
    kt = np.array([k[0] for k in keys], float)
    kv = np.array([k[1] for k in keys], float)
    h = 0.05
    lo, hi = min(t.min(), kt[0]) - width, max(t.max(), kt[-1]) + width
    g = np.arange(lo, hi + h, h)
    v = np.interp(g, kt, kv)
    n = max(3, int(round(width / h)) | 1)
    w = np.hanning(n + 2)[1:-1]
    w /= w.sum()
    pad = n // 2
    vp = np.concatenate([np.full(pad, v[0]), v, np.full(pad, v[-1])])
    sm = np.convolve(vp, w, mode="valid")
    return np.interp(t, g, sm)


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
        """(time, strength) of the passage of the storm at a station, or None without a usable series.

        A rain-rate maximum of at least 5 mm/h (a disdrometer) counts, strength = the rate in mm/h; otherwise the
        steepest pressure rise over 60 s of at least 1 hPa (the gust front of a pressure series), strength = 10 x the
        rise in hPa; otherwise the maximum gust or wind speed, strength = m/s.
        """
        s = st.get("series") or {}
        t = np.asarray(s.get("t_s") or [], float)
        if len(t) < 3:
            return None

        def arr(key):
            v = s.get(key)
            return None if not v else np.array([np.nan if x is None else x for x in v], float)

        rr = arr("rain_rate_mmh")
        if rr is not None and np.isfinite(rr).any() and np.nanmax(rr) >= 5.0:
            i = int(np.nanargmax(rr))
            return float(t[i]), float(rr[i])
        pr = arr("pressure_hpa")
        if pr is not None and np.isfinite(pr).sum() > 5:
            ok = np.isfinite(pr)
            tt, pp = t[ok], pr[ok]
            if len(tt) > 5 and tt[-1] - tt[0] > 120:
                ahead = np.interp(tt + 60.0, tt, pp)
                rise = ahead - pp
                rise[tt + 60.0 > tt[-1]] = -np.inf
                j = int(np.argmax(rise))
                if rise[j] >= 1.0:
                    # the passage is where the pressure rises fastest inside that minute
                    grad = np.gradient(pp, tt)
                    inw = (tt >= tt[j]) & (tt <= tt[j] + 60.0)
                    k = int(np.flatnonzero(inw)[np.argmax(grad[inw])])
                    return float(tt[k]), float(10.0 * rise[j])
        for key in ("wind_gust_ms", "wind_speed_ms"):
            w = arr(key)
            if w is not None and np.isfinite(w).any():
                i = int(np.nanargmax(w))
                return float(t[i]), float(w[i])
        return None


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

        t_a = tor_c - 12.0
        # the camera closes in on the vortex: 2.6 km, then 0.9 km, then 250-300 m for the real-time core
        # (tor_c +- 5 s), then backs off a little; low over the ground, looking up at the funnel
        keys_d = [
            (t_a, 2.6),
            (t_a + 4.5, 0.9),
            (tor_c - 4.0, 0.34),
            (tor_c, 0.26),
            (tor_c + 4.0, 0.30),
            (tor_c + 12.0, 0.8),
        ]
        keys_h = [
            (t_a, 0.35),
            (t_a + 4.5, 0.12),
            (tor_c - 4.0, 0.04),
            (tor_c + 4.0, 0.035),
            (tor_c + 12.0, 0.12),
        ]
        keys_l = [
            (t_a, 0.5),
            (t_a + 4.5, 0.35),
            (tor_c - 4.0, 0.16),
            (tor_c + 4.0, 0.16),
            (tor_c + 12.0, 0.3),
        ]

        def tor(t):
            q, _act = scene.tornado_at(data_time_of_clip(tl, t))
            u = np.clip((t - t_a) / 24.0, 0, 1)
            # AHEAD of the tornado (the line is behind it), orbiting slowly around it
            ang = np.radians(scene.perp_deg) + np.radians(-32.0 + 64.0 * smoothstep(u))
            dist = smooth_keys(t, keys_d)
            P = q + col(dist) * np.stack([np.sin(ang), np.cos(ang)], -1)
            return P, smooth_keys(t, keys_h), q, smooth_keys(t, keys_l)

        shots.append(
            Shot(
                "tornado",
                "Tornado: close, low, the line behind",
                t_a,
                tor_c + 12.0,
                tor,
                1.0,
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


def immersion(shots: list[Shot], t: np.ndarray, overlap: float = 5.0) -> np.ndarray:
    """1 inside the tornado shot, 0 elsewhere, crossfading with the same smoothstep as the shots (and the layers)."""
    w = np.zeros_like(t, dtype=float)
    for s in shots:
        if s.name == "tornado":
            up = smoothstep((t - (s.t0 - overlap / 2.0)) / overlap)
            down = smoothstep((t - (s.t1 - overlap / 2.0)) / overlap)
            w = np.maximum(w, up * (1.0 - down))
    return w


WIDE_FOV = 64.0  # vertical field of view of the immersive tornado shot, degrees
SHAKE = {"pos_m": 0.45, "heading_deg": 0.32, "pitch_deg": 0.26}  # amplitudes of the hand-held look


def shake_signal(t: np.ndarray, seed: float) -> np.ndarray:
    """Bounded, smooth, deterministic hand-held shake in [-1.6, 1.6] (three slow sines, no randomness)."""
    return (
        np.sin(2 * np.pi * 0.83 * t + seed)
        + 0.5 * np.sin(2 * np.pi * 1.71 * t + 2.0 * seed + 1.0)
        + 0.25 * np.sin(2 * np.pi * 3.07 * t + 3.0 * seed + 2.0)
    ) / 1.75 * 1.6


def build_camera(
    scene: Scene, tl: dict, shots: list[Shot], shake: bool = True
) -> list[dict]:
    t = tl["t_clip_s"]
    P, alt, T, tz, vex, idx = blend_shots(shots, t)
    d = T - P
    horiz = np.hypot(d[:, 0], d[:, 1])
    heading = bearing_deg(d[:, 0], d[:, 1])
    # the renderer stretches heights by the vertical exaggeration of the frame: so does the pitch
    pitch = np.degrees(np.arctan2((tz - alt) * vex, np.maximum(horiz, 1e-6)))
    if shake:
        w = immersion(shots, t)
        if w.max() > 0:
            # hand-held look inside the tornado shot: a few cm to decimetres of position, a third of a degree of aim
            P = P + w[:, None] * np.stack(
                [shake_signal(t, 0.4), shake_signal(t, 1.7)], -1
            ) * (SHAKE["pos_m"] / 1000.0)
            alt = alt + w * shake_signal(t, 2.9) * (SHAKE["pos_m"] / 2000.0)
            alt = np.maximum(alt, 0.0021)
            heading = (heading + w * shake_signal(t, 3.3) * SHAKE["heading_deg"]) % 360.0
            pitch = pitch + w * shake_signal(t, 4.1) * SHAKE["pitch_deg"]
    pitch = np.clip(pitch, -86.0, 86.0)
    # a wider lens inside the tornado shot (a phone-like first-person view); 45 degrees (the renderer's default) elsewhere
    fov = 45.0 + (WIDE_FOV - 45.0) * immersion(shots, t)
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
                "fov_deg": round(float(fov[i]), 3),
                "shot": shots[min(idx[i], len(shots) - 1)].name,
            }
        )
    return out


# ── nearest station, events ─────────────────────────────────────────────────


def nearest_station(stations: list[dict], x: float, y: float, z_km: float) -> str | None:
    """Id of the station nearest to the eye (3-D distance in km, stations on the ground)."""
    best, bd = None, 1e18
    for s in stations:
        if s.get("moving"):
            continue  # a vehicle is not a place to stand
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
        # the rotation of the vortex (vortex.py): at the middle of the tornado shot
        sh = next((s for s in shots if s.name == "tornado"), None)
        if sh is not None:
            tm = 0.5 * (sh.t0 + sh.t1)
            tdm = float(data_time_of_clip(tl, tm))
            qm, _a = scene.tornado_at(tdm)
            vp = vortex_params(tors[0], p.get("rotation") or [], tdm, float(qm[0]), float(qm[1]))
            if vp is not None:
                sp = tl["speedup"]
                i0 = int(round(sh.t0 * tl["fps"]))
                i1 = int(round(sh.t1 * tl["fps"]))
                real = [
                    float(tl["t_clip_s"][i])
                    for i in range(i0, min(i1, len(sp)))
                    if sp[i] <= 1.3
                ]
                ev["tornado"]["vortex"] = {
                    **{k: (round(v, 5) if isinstance(v, float) else v) for k, v in vp.items()},
                    "at_clip_s": round(tm, 3),
                    "real_time_from_clip_s": round(min(real), 3) if real else None,
                    "real_time_to_clip_s": round(max(real), 3) if real else None,
                    "speedup_at_middle": round(float(np.interp(tm, tl["t_clip_s"], sp)), 3),
                    "rotation_deg_per_clip_s_at_middle": round(
                        math.degrees(vp["omega_rad_s"])
                        * float(np.interp(tm, tl["t_clip_s"], sp)),
                        2,
                    ),
                    "note": "stylised funnel: position from the survey, spin from the survey wind range and half the path width; "
                    "the radar resolves only the mesocyclone-scale couplet (vrot_radar_ms, meso_*)",
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
    # the data's own events (science_events.json) in clip time, so the sound can follow them
    ev["science"] = []
    for e in p.get("science", []):
        ev["science"].append(
            {
                "kind": e["kind"],
                "t_data_s": e["t_s"],
                "t_clip_s": round(float(clip_time_of_data(tl, e["t_s"])), 3),
                "numbers": e.get("numbers"),
                "class": e.get("class"),
                "where": e.get("where"),
                "lat": e.get("lat"),
                "lon": e.get("lon"),
            }
        )
    # radar hail cores: per observed step the cells classified as hail, their centroid and count
    ev["hail_cores"] = []
    for k, h in enumerate(p.get("hail", [])):
        if h and h.get("n", 0) > 0 and h.get("p"):
            pts = np.asarray(h["p"], float)
            la, lo = geo.latlon(pts[:, 0].mean(), pts[:, 1].mean())
            td = float(p["steps"][k]["t_s"])
            ev["hail_cores"].append(
                {
                    "t_data_s": td,
                    "t_clip_s": round(float(clip_time_of_data(tl, td)), 3),
                    "n_cells": int(h["n"]),
                    "lat": round(float(la), 5),
                    "lon": round(float(lo), 5),
                    "max_height_km": round(float(pts[:, 2].max()), 2),
                    "class": "retrieved",
                }
            )
    # the onset of heavy rain at the disdrometers (OBSERVED rain rate crossing 10 mm/h)
    ev["rain"] = []
    for s in p.get("stations", []):
        ser = s.get("series") or {}
        ts, rr = ser.get("t_s"), ser.get("rain_rate_mmh")
        if not ts or not rr:
            continue
        for i, v in enumerate(rr):
            if v is not None and v >= 10.0 and (i == 0 or rr[i - 1] is None or rr[i - 1] < 10.0):
                ev["rain"].append(
                    {
                        "station": s["id"],
                        "t_data_s": ts[i],
                        "t_clip_s": round(float(clip_time_of_data(tl, ts[i])), 3),
                        "rain_rate_mmh": v,
                        "lat": s["lat"],
                        "lon": s["lon"],
                        "class": "observed",
                    }
                )
                break
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
    """The station of the ground-level shot [t0, t1].

    A station whose storm passage (rain-rate maximum, gust-front pressure rise or wind maximum, see
    ``Scene.station_arrival``) falls inside the shot wins, the stronger and the closer to the tornado path the
    better; with none inside, the one whose passage is nearest in time to the shot.
    """
    sts = scene.p.get("stations", [])
    best, score = None, -1e18
    ta, tb = float(data_time_of_clip(tl, t0)), float(data_time_of_clip(tl, t1))
    tor = [t for t in scene.p.get("tornadoes", []) if len(t.get("path", [])) >= 2]
    path = np.array([[q["x"], q["y"]] for q in tor[0]["path"]]) if tor else None
    for s in sts:
        if s.get("moving"):
            continue
        arr = scene.station_arrival(s)
        if arr is None:
            continue
        tm, strength = arr
        inside = ta + 0.25 * (tb - ta) <= tm <= tb - 0.15 * (tb - ta)
        dist = (
            float(np.min(np.hypot(path[:, 0] - s["x"], path[:, 1] - s["y"])))
            if path is not None
            else 0.0
        )
        sc = (
            (1e6 if inside else -abs(tm - (ta + 0.6 * (tb - ta))))
            + min(strength, 100.0)
            - 0.5 * dist
        )
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
    core_speedup: float | None = 1.0,
    core_width: float = 10.0,
) -> dict:
    """Everything: timeline.json, camera_path.json and events.json contents.

    The tornado shot is slowed to ``dip_level`` of the base speed and its middle ``core_width`` seconds run at
    ``core_speedup`` (1.0 = real time), so that the rotation of the vortex is paced as in nature.
    """
    scene = Scene(perils)
    window = scene.window_s
    span = scene.tornado_span()
    tor_c = None
    if span:
        mid = 0.5 * (span[0] + span[1])
        tor_c = solve_dip_centre(
            window,
            mid,
            duration,
            fps,
            36.0,
            duration - 62.0,
            dip_width,
            dip_level,
            core_speedup,
            core_width,
        )
    tl = build_timeline(
        window, duration, fps, tor_c, dip_width, dip_level, core_speedup, core_width
    )
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
        else {
            "centre_clip_s": tor_c,
            "width_s": dip_width,
            "level": dip_level,
            "core_width_s": core_width if core_speedup is not None else None,
            "core_speedup": core_speedup,
        },
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

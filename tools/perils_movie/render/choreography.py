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
EYE_TOR_M = 2.0  # eye height of the standing observer of the tornado shot, m
DEFAULT_FOV = 45.0  # vertical field of view of the renderer's default lens, degrees
TOR_SHOT_S = 24.0  # length of the tornado shot, s
TOR_BLEND_S = 8.0  # the camera glides into the tornado shot over this many seconds (slower than the others: it comes from 60 km)
TOR_SIDE = -1.0  # the observer is to the right of the direction of travel
CHASER_LATERAL_KM = 0.42  # distance of the chaser from the track, km
DEBRIS_MISS_KM = 0.075  # distance at which the tornado passes the debris-field observer, km
PASS_TAU = 18.5  # seconds into the shot at which it passes
DOLLY_START, DOLLY_S = 11.0, 5.0  # the move from the chaser to the debris-field position


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
    n = max(3, round(width / h) | 1)
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

    def tornado_axes(self):
        """Unit vectors (x east, y north) along the tornado's direction of travel and to its left."""
        tors = [t for t in self.p.get("tornadoes", []) if len(t.get("path", [])) >= 2]
        path = tors[0]["path"]
        d = np.array([path[-1]["x"] - path[0]["x"], path[-1]["y"] - path[0]["y"]], float)
        u = d / max(np.hypot(*d), 1e-9)
        return u, np.array([-u[1], u[0]])

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
    def __init__(
        self,
        name: str,
        label: str,
        t0: float,
        t1: float,
        fn,
        vexag: float,
        overlap: float | None = None,
        fov: float | object = DEFAULT_FOV,
        shake: float = 0.0,
        arc_km: float = 0.0,
        immersion: float = 0.0,
    ):
        self.name, self.label, self.t0, self.t1, self.fn, self.vexag = (
            name,
            label,
            t0,
            t1,
            fn,
            vexag,
        )
        self.overlap = (
            overlap  # seconds of the crossfade into this shot (None: the default of blend_shots)
        )
        self.fov = fov  # vertical field of view, degrees: a number, or a function of the clip times
        self.shake = shake  # weight of the hand-held look (0 none .. 1 full)
        self.arc_km = (
            arc_km  # extra height the camera rises to in the middle of the glide into this shot
        )
        self.immersion = immersion  # 0..1: how immersive the shot is (labels, grade): for the sound and the tests

    def fov_at(self, t) -> np.ndarray:
        t = np.asarray(t, float)
        return np.asarray(
            self.fov(t) if callable(self.fov) else np.full(len(t), float(self.fov)), float
        )


def plan_shots(
    scene: Scene,
    tl: dict,
    duration: float,
    tor_c: float | None,
    station: dict | None,
    focus: list[Shot] | None = None,
    layout: dict | None = None,
) -> list[Shot]:
    """The shot list. Times are clip seconds; ``tor_c`` is the clip time of the tornado shot's middle (or None).

    With ``focus`` (the five angles of focus.py) they replace the single tornado shot. ``layout`` gives the clip times
    of the other shots: ``est_end`` (the flyby starts), ``fly_end`` (it ends, the first focus angle begins), ``win_s`` and
    ``rise_s`` (lengths of the ground-level and the rising shot), ``top`` = (centre km, (height0, height1) km) of the finale
    when it is to look at the track instead of the line."""
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

    lay = layout or {}
    b0 = lay.get("est_end", 15.0)
    b1 = lay.get("fly_end", (tor_c - 12.0) if tor_c is not None else 40.0)
    if focus:
        tor_c = b1  # the flyby ends at the first angle, near the tornado

    # the flyby runs along the line from its north end toward where the tornado is, and ends a few km from it
    if tor_c is not None:
        td_b1 = float(data_time_of_clip(tl, b1))
        c_b1 = scene.centre(td_b1)
        tor_b1 = scene.tornado_at(td_b1)[0]
        s_end = float(np.clip((tor_b1 - c_b1) @ a, -0.7 * L, 0.7 * L))
        d_end = float((tor_b1 - c_b1) @ m) + 5.0
        look_end = tor_b1
    else:
        s_end, d_end, look_end = -0.62 * L, front + 24.0, None

    def fly(t):
        u = np.clip((t - b0) / max(1e-6, b1 - b0), 0, 1)
        w = smoothstep(u)
        s = (1 - w) * 0.62 * L + w * s_end  # along-line position (km)
        c = cen(t)
        d = (1 - w) * (front + 24.0 + 4.0 * np.sin(u * math.pi)) + w * d_end
        P = c + col(s) * a[None, :] + col(d) * m[None, :]
        look = c + col(s - 0.22 * L) * a[None, :] + (front - 6.0) * m[None, :]
        if look_end is not None:
            look = (1 - w)[:, None] * look + w[:, None] * look_end[None, :]
        return P, 2.2 + 1.3 * np.sin(u * math.pi), look, _pad(3.2, len(t))

    shots = [
        Shot("establishing", "Establishing view of the whole line", 0.0, b0, estab, 2.4),
        Shot("flyby", "Low flyby along the line", b0, b1, fly, 2.4),
    ]
    t_after = b1
    if focus:
        shots.extend(focus)
        t_after = focus[-1].t1
    elif tor_c is not None:
        t_a = tor_c - 12.0
        u_t, n_t = scene.tornado_axes()
        td_a = float(data_time_of_clip(tl, t_a))
        a0 = scene.tornado_at(td_a)[0]
        a_pass = scene.tornado_at(float(data_time_of_clip(tl, t_a + PASS_TAU)))[0]

        def tor(t):
            """A chaser 800-500 m ahead of and beside the tornado, then (a dolly of 5 s) the debris field, 75 m from where
            it passes. The tornado simulation's camera modes (src/lib/utils/tornado/cameraModes.ts), in the film's frame."""
            tau = np.clip(np.asarray(t, float) - t_a, 0.0, TOR_SHOT_S)
            a_now = scene.tornado_at(data_time_of_clip(tl, t))[0]
            a_lag = scene.tornado_at(data_time_of_clip(tl, np.asarray(t, float) - 0.9))[0]
            ahead = 0.78 + (0.52 - 0.78) * smoothstep(tau / TOR_SHOT_S)
            pc = (
                a0[None, :]
                + col(ahead + 0.006 * tau) * u_t[None, :]
                + TOR_SIDE * CHASER_LATERAL_KM * n_t[None, :]
            )
            pd = np.broadcast_to(a_pass + TOR_SIDE * DEBRIS_MISS_KM * n_t, pc.shape)
            w = tornado_dolly(np.asarray(t, float), t_a)
            P = (1 - w)[:, None] * pc + w[:, None] * pd
            T = (1 - w)[:, None] * a_now + w[:, None] * a_lag
            tz = (1 - w) * (0.055 + 0.025 * np.sin(0.2 * tau)) + w * (
                0.016 + 0.022 * smoothstep(tau / TOR_SHOT_S)
            )
            return P, _pad(EYE_TOR_M / 1000.0, len(tau)), T, tz

        shots.append(
            Shot(
                "tornado",
                "Tornado: close, low, the line behind",
                t_a,
                tor_c + 12.0,
                tor,
                1.0,
                overlap=TOR_BLEND_S,
                fov=lambda t, _t_a=t_a: (
                    FOV_CHASER + (FOV_DEBRIS - FOV_CHASER) * tornado_dolly(t, _t_a)
                ),
                shake=1.0,
                immersion=1.0,
            )
        )
        t_after = tor_c + 12.0
    t_st1 = min(duration - 30.0, t_after + lay.get("win_s", 27.0))
    if station is not None:
        sx, sy = station["x"], station["y"]
        look_b = (scene.perp_deg + 180.0) % 360.0  # looking back toward the approaching line

        long_shot = (t_st1 - t_after) >= 20.0

        def stat(t):
            u = np.clip((t - t_after) / max(1e-6, t_st1 - t_after), 0, 1)
            # a slow pan while the line approaches; in a long shot, after it has passed, a turn to watch it leave (a short shot only
            # drifts round by 70 degrees: a turn of 180 degrees in a few seconds is a whip)
            if long_shot:
                turn = 14.0 * (1.0 - 2.0 * smoothstep(u / 0.7)) + 180.0 * smoothstep(
                    (u - 0.7) / 0.3
                )
            else:
                turn = 14.0 * (1.0 - 2.0 * smoothstep(u / 0.7)) + 70.0 * smoothstep((u - 0.3) / 0.7)
            yaw = np.radians(look_b + turn)
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
    e1 = duration - lay.get("top_s", 15.0)
    if "rise_s" in lay and station is not None:
        e1 = t_st1 + lay["rise_s"]

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
        if lay.get("top"):
            ctr, (h0, h1) = np.asarray(lay["top"][0], float), lay["top"][1]
            cc = np.broadcast_to(ctr, c.shape)
            return (
                cc,
                h0 + (h1 - h0) * smoothstep(u),
                cc + 0.35 * np.array([0.0, 1.0]),
                _pad(0.0, len(t)),
            )
        return c, 62.0 + 16.0 * smoothstep(u), c + 7.0 * m, _pad(0.0, len(t))

    shots.append(Shot("topdown", "Top-down: the line from above", e1, duration, top, 3.0))
    return shots


def blend_shots(shots: list[Shot], t: np.ndarray, overlap: float = 5.0):
    """P (km, scene), eye altitude (km), look-at point and its height, vexag, shot index, heading, pitch, field of view
    and the weight of the hand-held look per frame.

    Shots crossfade in sequence: shot j fades in over ``overlap`` seconds centred on its start
    (smoothstep weight), on top of the blend of the shots before it. Every shot is continuous with a
    continuous first derivative, and so is each weight, so the result is too. The aim (heading, pitch) is
    blended as an aim (the short way round), not recomputed from the blended look-at point: a point far
    behind a nearly arrived camera would otherwise swing the view fast.
    """
    t = np.asarray(t, float)
    n = len(t)
    first = shots[0].fn(np.clip(t, shots[0].t0 - overlap, shots[0].t1 + overlap))
    fov = shots[0].fov_at(t)
    shake = np.full(n, float(shots[0].shake))
    P = first[0].astype(float).copy()
    alt = np.asarray(first[1], float).copy()
    T = first[2].astype(float).copy()
    tz = np.asarray(first[3], float).copy()
    vex = np.full(n, shots[0].vexag, float)
    idx = np.zeros(n, int)

    def aim(p_, alt_, t_, tz_, vex_):
        d_ = np.asarray(t_, float) - np.asarray(p_, float)
        hz = np.hypot(d_[:, 0], d_[:, 1])
        return bearing_deg(d_[:, 0], d_[:, 1]), np.degrees(
            np.arctan2(
                (np.asarray(tz_, float) - np.asarray(alt_, float)) * vex_, np.maximum(hz, 1e-6)
            )
        )

    heading, pitch = aim(P, alt, T, tz, vex)
    heading = np.degrees(np.unwrap(np.radians(heading)))  # continuous in time (no wrap at 360)
    for j, s in enumerate(shots[1:], start=1):
        ov = s.overlap or overlap
        w = smoothstep((t - (s.t0 - ov / 2.0)) / ov)
        ev = s.fn(np.clip(t, s.t0 - ov, s.t1 + ov))
        P = (1 - w)[:, None] * P + w[:, None] * ev[0]
        alt = (1 - w) * alt + w * np.asarray(ev[1], float)
        T = (1 - w)[:, None] * T + w[:, None] * ev[2]
        tz = (1 - w) * tz + w * np.asarray(ev[3], float)
        vex = (1 - w) * vex + w * s.vexag
        fov = (1 - w) * fov + w * s.fov_at(t)
        shake = (1 - w) * shake + w * s.shake
        alt = alt + s.arc_km * 4.0 * w * (1 - w)
        idx = np.where(w >= 0.5, j, idx)
        hj, pj = aim(ev[0], ev[1], ev[2], ev[3], s.vexag)
        # the aim is blended the short way round AT THE MIDDLE of the glide, and continuously through it: a difference that crosses 180
        # degrees during the glide (the two aims nearly opposite) would otherwise flip the blended heading by a whole turn in one frame
        hj = np.degrees(np.unwrap(np.radians(hj)))
        mid = int(np.argmax(w >= 0.5)) if (w >= 0.5).any() else n - 1
        hj = hj + 360.0 * np.round((heading[mid] - hj[mid]) / 360.0)
        heading = heading + (hj - heading) * w
        pitch = (1 - w) * pitch + w * pj
    heading = heading % 360.0
    return P, alt, T, tz, vex, idx, heading, pitch, fov, shake


def tornado_dolly(t, t_a: float):
    """0 for the chaser, 1 for the debris-field view: a smooth move that starts ``DOLLY_START`` s into the shot."""
    return smoothstep((np.asarray(t, float) - t_a - DOLLY_START) / DOLLY_S)


def crossfade_values(shots: list[Shot], values, t, overlap: float = 5.0) -> np.ndarray:
    """A per-shot value as a function of clip time: shot j fades in over its overlap, centred on its start (the page's ``crossfade``)."""
    t = np.asarray(t, float)
    v = np.full(len(t), float(values[0]))
    for j in range(1, len(shots)):
        ov = shots[j].overlap or overlap
        w = smoothstep((t - (shots[j].t0 - ov / 2.0)) / ov)
        v = (1 - w) * v + w * float(values[j])
    return v


def immersion(shots: list[Shot], t: np.ndarray, overlap: float = 5.0) -> np.ndarray:
    """0..1 how immersive the shot in charge is (the tornado shots 1), crossfading with the same smoothstep as the shots."""
    return crossfade_values(shots, [s.immersion for s in shots], t, overlap)


FOV_CHASER, FOV_DEBRIS = (
    38.0,
    62.0,
)  # vertical field of view of the chaser and of the debris-field view, degrees
WIDE_FOV = FOV_DEBRIS
SHAKE = {
    "pos_m": 0.3,
    "heading_deg": 0.3,
    "pitch_deg": 0.25,
}  # base amplitudes of the hand-held look
SHAKE_NEAR = 2.8  # the shake grows to 1 + SHAKE_NEAR times the base at the tornado (bounded: about 1.1 degrees)


def shake_signal(t: np.ndarray, seed: float) -> np.ndarray:
    """Bounded, smooth, deterministic hand-held shake in [-1.6, 1.6] (three slow sines, no randomness)."""
    return (
        (
            np.sin(2 * np.pi * 0.83 * t + seed)
            + 0.5 * np.sin(2 * np.pi * 1.71 * t + 2.0 * seed + 1.0)
            + 0.25 * np.sin(2 * np.pi * 3.07 * t + 3.0 * seed + 2.0)
        )
        / 1.75
        * 1.6
    )


def build_camera(scene: Scene, tl: dict, shots: list[Shot], shake: bool = True) -> list[dict]:
    t = tl["t_clip_s"]
    P, alt, _T, _tz, vex, idx, heading, pitch, fov, wshake = blend_shots(shots, t)
    # the renderer stretches heights by the vertical exaggeration of the frame: so does the pitch (blend_shots)
    if shake and wshake.max() > 0:
        # hand-held look in the shots that ask for it: stronger where the tornado is near (it is bounded)
        near = np.zeros(len(t))
        if scene.p.get("tornadoes"):
            a_xy = scene.tornado_at(data_time_of_clip(tl, t))[0]
            near = np.exp(-((np.hypot(P[:, 0] - a_xy[:, 0], P[:, 1] - a_xy[:, 1]) / 0.15) ** 2))
        g = wshake * (1.0 + SHAKE_NEAR * near) / (1.0 + SHAKE_NEAR)
        P = P + g[:, None] * np.stack([shake_signal(t, 0.4), shake_signal(t, 1.7)], -1) * (
            SHAKE["pos_m"] / 1000.0
        )
        alt = alt + g * shake_signal(t, 2.9) * (SHAKE["pos_m"] / 2000.0)
        alt = np.where(wshake > 0.01, np.maximum(alt, 0.0021), alt)
        heading = (
            heading + g * shake_signal(t, 3.3) * SHAKE["heading_deg"] * (1.0 + SHAKE_NEAR)
        ) % 360.0
        pitch = pitch + g * shake_signal(t, 4.1) * SHAKE["pitch_deg"] * (1.0 + SHAKE_NEAR)
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
        # the rotation of the vortex (vortex.py): at the middle of the tornado shot (or of the real-time angles of the focus)
        sh = next((s for s in shots if s.name == "tornado"), None)
        foc = [s for s in shots if s.name.startswith("focus_")]
        if sh is None and foc:
            sh = Shot("focus", "Tornado focus", foc[0].t0, foc[-1].t1, None, 1.0)
        if sh is not None:
            ev["tornado"]["shot"] = {
                "t0_clip_s": round(sh.t0, 3),
                "t1_clip_s": round(sh.t1, 3),
                "t0_data_s": round(float(data_time_of_clip(tl, sh.t0)), 3),
                "t1_data_s": round(float(data_time_of_clip(tl, sh.t1)), 3),
            }
            tm = 0.5 * (sh.t0 + sh.t1)
            if foc:
                rt = [s for s in foc if s.name in ("focus_chaser", "focus_close", "focus_low")]
                if rt:
                    tm = 0.5 * (rt[0].t0 + rt[-1].t1)
            tdm = float(data_time_of_clip(tl, tm))
            _q, _a = scene.tornado_at(tdm)
            vp = vortex_params(tors[0], p.get("vortex"), tdm)
            if vp is not None:
                sp = tl["speedup"]
                i0 = round(sh.t0 * tl["fps"])
                i1 = round(sh.t1 * tl["fps"])
                real = [
                    float(tl["t_clip_s"][i]) for i in range(i0, min(i1, len(sp))) if sp[i] <= 1.3
                ]
                ev["tornado"]["vortex"] = {
                    **{
                        k: (round(v, 5) if isinstance(v, float) else v)
                        for k, v in vp.items()
                        if k != "parent_circulation"
                    },
                    "parent_circulation": vp["parent_circulation"],
                    "at_clip_s": round(tm, 3),
                    "real_time_from_clip_s": round(min(real), 3) if real else None,
                    "real_time_to_clip_s": round(max(real), 3) if real else None,
                    "speedup_at_middle": round(float(np.interp(tm, tl["t_clip_s"], sp)), 3),
                    "rotation_deg_per_clip_s_at_middle": round(
                        math.degrees(vp["omega_rad_s"]) * float(np.interp(tm, tl["t_clip_s"], sp)),
                        2,
                    ),
                    "note": "stylised funnel: position from the survey, spin ESTIMATED from the survey wind range and half the "
                    "damage width; no radar resolves the tornado (366 m wide, 500 m grid): the radars measured the parent "
                    "circulation (parent_circulation)",
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
        {
            "name": s.name,
            "label": s.label,
            "t0": round(s.t0, 3),
            "t1": round(s.t1, 3),
            "overlap": s.overlap,
        }
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
    core_width: float = TOR_SHOT_S,
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

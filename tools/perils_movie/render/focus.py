"""The tornado-focus choreography of the PERiLS movie (docs/PERILS_MOVIE.md, "Tornado focus").

Everything is placed from the VERIFIED geometry (tools/perils_movie/render/verify_geometry.py, verification_geometry.md):
the surveyed track (straight, begin to end, constant speed), the direction of travel of the tornado, the line motion
(the echo pattern), the side of the inflow (the air flows in from the south, storm-relative) and the strong echo (to the
north of the tornado). One position for the tornado, from the survey, is used by the camera, the simulation, the inset and
the events.

Six angles, in the frame of the tornado (``travel`` = along the track, ``right`` = to the right of the travel = the
inflow / rain-free side):

1. WIDE, from the inflow side, 14 -> 9 km away and 2.5 -> 2 km high, 60 degree lens: the parent echo above the tornado. Time-lapse.
2. CHASER, a standing observer on the right of the track 1.25 km away, 12 m high, 44 degree lens: the whole funnel and the
   wall cloud under the echo, the tornado crossing the frame. Real time (touchdown).
3. CLOSE, side-on 450 m from the track, 2 m high, 62 degree lens: the funnel passes across the frame. Real time.
4. LOW, 340 m from the track, 1.5 m high, looking up 30 degrees with a 68 degree lens. Real time.
5. SWATH, a chase camera 420 m behind and 200 m to the right of the tornado, 90 m high, looking down at the damage path it has
   just made (the simulation's trees falling) with the funnel ahead. Real time.
6. TOP, the whole track and the echo from the right of it, 9 km away and 4.8 km high, the damage swath growing across the frame
   (in the stand-alone clip it then rises to look straight down on the track). Time-lapse.

The data clock is built from segments: real time (x1) around the close shots, "lapses" between them that carry the
camera from one place to the next while the data clock speeds up and slows down again, and cruises elsewhere. The speed-up
is on screen (the HUD).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from choreography import (
    Scene,
    Shot,
    bearing_deg,
    data_time_of_clip,
    smoothstep,
    unit,
)

# ── geometry of the five angles ─────────────────────────────────────────────

INFLOW_BEARING = 190.0  # degrees: the storm-relative inflow comes from the south (verification_geometry.md, winds at 0.5 km)
WIDE_DIST_KM = (
    14.0,
    9.0,
)  # km from the tornado at the start and end of the wide shot: outside the 40 dBZ echo, in the inflow air
WIDE_ALT_KM = (2.5, 2.0)
WIDE_BEARING = 170.0  # degrees, from the tornado to the camera: south, the inflow side
WIDE_FOV = 60.0
WIDE_AIM_NORTH_KM = (
    1.0,
    0.5,
)  # the aim point is this far north of the tornado (the echo above it), start and end
WIDE_AIM_Z_KM = (3.0, 2.5)  # and this high, km
CHASER_DIST_KM = 1.25
CHASER_BEARING_OFF = 15.0  # degrees south of the right-hand perpendicular: the strong echo (north) stays in the frame
CHASER_ALT_M = 12.0
CHASER_FOV = 44.0
CHASER_FOLLOW = 0.75  # share of the tornado's motion the observer pans after
CLOSE_DIST_KM = 0.45
CLOSE_ALT_M = 2.0
CLOSE_FOV = 62.0
CLOSE_FOLLOW = 0.4
LOW_LATERAL_KM = 0.34
LOW_AHEAD_KM = 0.12
LOW_ALT_M = 1.5
LOW_FOV = 68.0
SWATH_BEHIND_KM = 0.42
SWATH_SIDE_KM = 0.2
SWATH_ALT_M = 90.0
SWATH_FOV = 62.0
TOP_FOV = 50.0
TOP_OBLIQUE = (
    9.0,
    4.8,
    0.0,
)  # km from the middle of the track, km high, degrees from the right-hand perpendicular: the oblique view
TOP_AIM_Z_KM = 0.5
TOP_END_ALT_KM = 10.5  # of the top-down end (the whole track in view) when the shot ends in it


@dataclass
class Seg:
    """A stretch of the clip: ``length`` clip seconds from data time ``td0`` (seconds after the window start).

    kind ``rt`` runs the data clock at 1 (one data second per clip second); ``lapse`` starts and ends at x1 and speeds up
    in between so that it reaches the next segment's ``td0``; ``cruise`` runs at a constant speed to the next ``td0``."""

    name: str
    length: float
    kind: str
    td0: float


def _bump(tau):
    tau = np.asarray(tau, float)
    return smoothstep(tau / 0.3) * smoothstep((1.0 - tau) / 0.3)


_BUMP_INT = float(np.mean(_bump(np.linspace(0, 1, 2001))))


def _hann_smooth(x: np.ndarray, width: int) -> np.ndarray:
    width = max(3, width | 1)
    w = np.hanning(width + 2)[1:-1]
    w /= w.sum()
    pad = width // 2
    xp = np.concatenate([np.full(pad, x[0]), x, np.full(pad, x[-1])])
    return np.convolve(xp, w, mode="valid")


def segment_targets(segs: list[Seg], window_s: float) -> list[tuple[float, float]]:
    """(data time at the start, data time at the end) of every segment."""
    out = []
    for j, s in enumerate(segs):
        nxt = segs[j + 1].td0 if j + 1 < len(segs) else window_s
        end = s.td0 + s.length if s.kind == "rt" else nxt
        if end < s.td0 or (s.kind == "rt" and end > nxt + 1e-6):
            raise ValueError(
                f"segment {s.name}: data span {s.td0}..{end} does not fit before {nxt}"
            )
        out.append((s.td0, end))
    return out


def build_timeline_segments(
    segs: list[Seg], window_s: float, fps: int = 30, ramp_s: float = 1.5
) -> dict:
    """The dense clip -> data mapping of a list of segments (see ``Seg``); monotone, and every boundary after a ``cruise`` or a
    ``lapse`` is where the next segment's ``td0`` says (to ~0.01 s), the end of the clip at ``window_s``.

    The speed of the data clock is ``1 + A_j g_j(t)`` in segment ``j``: ``g`` is 0 in a real-time stretch (the speed is exactly
    1 there), a smooth bump in a lapse, and 1 in a cruise except for ``ramp_s``-second ramps to 1 where it meets a real-time
    stretch or a lapse (so the speed is continuous everywhere); ``A_j`` is solved for the data span of the segment."""
    duration = float(sum(s.length for s in segs))
    n = round(duration * fps)
    t = np.arange(n + 1) / fps
    edges = np.concatenate([[0.0], np.cumsum([s.length for s in segs])])
    tg = segment_targets(segs, window_s)
    seg_of = np.minimum(np.searchsorted(edges, t, side="right") - 1, len(segs) - 1)
    g = np.zeros(len(t))
    for j, s in enumerate(segs):
        m = seg_of == j
        x = t[m] - edges[j]
        if s.kind == "lapse":
            g[m] = _bump(x / s.length)
        elif s.kind == "cruise":
            r = min(ramp_s, 0.35 * s.length)
            gj = np.ones(len(x))
            slow = (
                tg[j][1] - tg[j][0]
            ) < s.length  # slower than real time: no ramps to 1 (they could not keep the span)
            if slow:
                pass
            elif j > 0 and segs[j - 1].kind in ("rt", "lapse"):
                gj *= smoothstep(x / r)
            if not slow and j + 1 < len(segs) and segs[j + 1].kind in ("rt", "lapse"):
                gj *= smoothstep((s.length - x) / r)
            g[m] = gj
    amp = np.zeros(len(segs))

    def cumulative():
        sp = 1.0 + g * amp[seg_of]
        return sp, segs[0].td0 + np.concatenate([[0.0], np.cumsum((sp[1:] + sp[:-1]) * 0.5 / fps)])

    for _ in range(4):
        for j, s in enumerate(segs):
            if s.kind == "rt":
                continue
            _sp, td = cumulative()
            a = float(np.interp(edges[j], t, td))
            b = float(np.interp(edges[j + 1], t, td))
            want = tg[j][1] - a  # to the absolute end of this segment
            got = b - a
            m = seg_of == j
            gint = float(np.sum(0.5 * (g[m][1:] + g[m][:-1])) / fps) if m.sum() > 1 else 1.0
            if gint > 1e-9:
                amp[j] += (want - got) / gint
    _sp, td = cumulative()
    if not np.all(np.diff(td) > 0):
        raise ValueError(
            "the data clock would run backwards: the segments ask for too little data in a lapse"
        )
    return {"fps": fps, "frames": n, "t_clip_s": t, "t_data_s": td, "speedup": np.gradient(td, t)}


# ── the tornado-centred rig ─────────────────────────────────────────────────


class Rig:
    """The surveyed tornado as the camera sees it: position at clip time, axes of the track."""

    def __init__(self, scene: Scene, tl: dict):
        self.scene, self.tl = scene, tl
        self.u, self.n = scene.tornado_axes()  # along the travel, to its left
        self.r = -self.n  # to its right: the inflow / rain-free side
        self.travel_deg = float(bearing_deg(self.u[0], self.u[1]))
        self.right_deg = (self.travel_deg + 90.0) % 360.0
        path = next(t["path"] for t in scene.p["tornadoes"] if len(t.get("path", [])) >= 2)
        self.begin = np.array([path[0]["x"], path[0]["y"]], float)
        self.end = np.array([path[-1]["x"], path[-1]["y"]], float)
        self.mid = 0.5 * (self.begin + self.end)

    def td(self, t):
        return data_time_of_clip(self.tl, np.asarray(t, float))

    def T(self, t) -> np.ndarray:  # noqa: N802
        """Surveyed tornado position (km) at clip time(s) ``t``, shape (N, 2)."""
        t = np.atleast_1d(np.asarray(t, float))
        return self.scene.tornado_at(self.td(t))[0]

    def at(self, t, bearing, dist_km) -> np.ndarray:
        """The point ``dist_km`` from the tornado toward ``bearing`` (degrees from north), shape (N, 2)."""
        return self.T(t) + float(dist_km) * unit(bearing)[None, :]


def _unit_rows(bearing) -> np.ndarray:
    """Unit vectors (east, north) of bearings (degrees), shape (N, 2)."""
    b = np.radians(np.atleast_1d(np.asarray(bearing, float)))
    return np.stack([np.sin(b), np.cos(b)], -1)


def _col(v, n):
    return np.broadcast_to(np.asarray(v, float), (n,)).copy()


def focus_shots(
    rig: Rig,
    beats: dict[str, tuple[float, float]],
    blend: dict[str, tuple[float, float]],
    end_top: bool,
) -> list[Shot]:
    """The five angles as shots. ``beats`` maps their names to the (start, end) of their stretch of the clip (clip seconds);
    ``blend`` to (the time the glide into the shot is centred on, its length)."""
    u, rgt = rig.u, rig.r
    N = np.array([0.0, 1.0])

    t0, t1 = beats["focus_wide"]

    def wide(t):
        w = smoothstep((t - t0) / max(1e-6, t1 - t0))
        d = WIDE_DIST_KM[0] + (WIDE_DIST_KM[1] - WIDE_DIST_KM[0]) * w
        beta = WIDE_BEARING + 14.0 * w
        P = rig.T(t) + d[:, None] * _unit_rows(beta)
        alt = WIDE_ALT_KM[0] + (WIDE_ALT_KM[1] - WIDE_ALT_KM[0]) * w
        look = (
            rig.T(t)
            + (WIDE_AIM_NORTH_KM[0] + (WIDE_AIM_NORTH_KM[1] - WIDE_AIM_NORTH_KM[0]) * w)[:, None]
            * N[None, :]
        )
        return P, alt, look, WIDE_AIM_Z_KM[0] + (WIDE_AIM_Z_KM[1] - WIDE_AIM_Z_KM[0]) * w

    a0, a1 = beats["focus_chaser"]
    tm_c = 0.5 * (a0 + a1)
    beta_c = rig.right_deg + CHASER_BEARING_OFF
    P_c = rig.at(tm_c, beta_c, CHASER_DIST_KM)[0]
    T_c = rig.T(tm_c)[0]

    def chaser(t):
        n = len(t)
        P = np.broadcast_to(P_c, (n, 2)).copy()
        look = T_c[None, :] + CHASER_FOLLOW * (rig.T(t) - T_c[None, :])
        return P, _col(CHASER_ALT_M / 1000.0, n), look, _col(0.32 + 0.02 * np.sin(0.2 * t), n)

    c0, c1 = beats["focus_close"]
    tm_x = 0.5 * (c0 + c1)
    P_x = rig.at(tm_x, rig.right_deg, CLOSE_DIST_KM)[0]
    T_x = rig.T(tm_x)[0]

    def close(t):
        n = len(t)
        P = np.broadcast_to(P_x, (n, 2)).copy()
        look = T_x[None, :] + CLOSE_FOLLOW * (rig.T(t) - T_x[None, :])
        return P, _col(CLOSE_ALT_M / 1000.0, n), look, _col(0.13, n)

    l0, l1 = beats["focus_low"]
    tm_l = 0.5 * (l0 + l1)
    T_l = rig.T(tm_l)[0]
    P_l = T_l + LOW_AHEAD_KM * u + LOW_LATERAL_KM * rgt

    def low(t):
        n = len(t)
        P = np.broadcast_to(P_l, (n, 2)).copy()
        look = rig.T(t)
        return P, _col(LOW_ALT_M / 1000.0, n), look, _col(0.24, n)

    def swath(t):
        """A chase camera behind and beside the tornado, high enough to see the path it has made."""
        n = len(t)
        T = rig.T(t)
        P = T - SWATH_BEHIND_KM * u[None, :] + SWATH_SIDE_KM * rgt[None, :]
        return P, _col(SWATH_ALT_M / 1000.0, n), T - 0.12 * u[None, :], _col(0.06, n)

    s0, s1 = beats["focus_top"]
    mid = rig.mid

    def top(t):
        """The whole track and the echo from the right of it (the inflow side): the camera stands still, looking at the middle of
        the track, so that the tornado marker and the damage swath cross the frame; in the stand-alone clip it then rises to look
        straight down on the whole track, north up."""
        n = len(t)
        d0, h0, b0 = TOP_OBLIQUE
        P0 = mid + d0 * _unit_rows(np.full(n, rig.right_deg + b0))
        if not end_top:
            return P0, _col(h0, n), np.broadcast_to(mid, (n, 2)).copy(), _col(TOP_AIM_Z_KM, n)
        w2 = smoothstep((t - (s0 + 0.35 * (s1 - s0))) / max(1e-6, 0.65 * (s1 - s0)))
        P = P0 * (1 - w2)[:, None] + (mid + 0.05 * _unit_rows(np.full(n, 0.0))) * w2[:, None]
        alt = h0 + (TOP_END_ALT_KM - h0) * w2
        look = mid[None, :] + (0.3 * w2)[:, None] * N[None, :]
        return P, alt, look, TOP_AIM_Z_KM * (1 - w2)

    names = ["focus_wide", "focus_chaser", "focus_close", "focus_low", "focus_swath", "focus_top"]
    bt = [blend[n][0] for n in names]
    t_ends = [*bt[1:], beats["focus_top"][1]]
    spec = {
        "focus_wide": (
            "Tornado focus: from the inflow side, the whole echo above it",
            wide,
            1.3,
            {"fov": WIDE_FOV, "immersion": 0.0},
        ),
        "focus_chaser": (
            "Tornado focus: a chaser's view from the inflow side",
            chaser,
            1.0,
            {"fov": CHASER_FOV, "shake": 0.5, "immersion": 1.0},
        ),
        "focus_close": (
            "Tornado focus: close, side-on",
            close,
            1.0,
            {"fov": CLOSE_FOV, "shake": 1.0, "arc_km": 0.12, "immersion": 1.0},
        ),
        "focus_low": (
            "Tornado focus: low, looking up",
            low,
            1.0,
            {"fov": LOW_FOV, "shake": 1.0, "arc_km": 0.12, "immersion": 1.0},
        ),
        "focus_swath": (
            "Tornado focus: looking back along the damage path",
            swath,
            1.0,
            {"fov": SWATH_FOV, "shake": 0.4, "immersion": 1.0},
        ),
        "focus_top": (
            "Tornado focus: the track from above",
            top,
            1.4,
            {"fov": TOP_FOV, "immersion": 0.0},
        ),
    }
    out = []
    for n, t_end in zip(names, t_ends, strict=True):
        label, fn, vex, kw = spec[n]
        out.append(Shot(n, label, blend[n][0], t_end, fn, vex, overlap=blend[n][1], **kw))
    return out


def layout_shots(
    rig: Rig, segs: list[Seg], ed: dict[str, tuple[float, float]], end_top: bool
) -> list[Shot]:
    """The six angles from a segment list: each dwells on its own stretch; the glide into the close and the low angle
    is centred on the lapse before it (its whole length), the others on the start of their stretch."""
    names = ("focus_wide", "focus_chaser", "focus_close", "focus_low", "focus_swath", "focus_top")
    beats = {n: ed[n] for n in names}
    blend: dict[str, tuple[float, float]] = {
        "focus_wide": (ed["focus_wide"][0], 5.0),
        "focus_chaser": (ed["focus_chaser"][0], 5.0),
    }
    for name, lap in (
        ("focus_close", "lapse1"),
        ("focus_low", "lapse2"),
        ("focus_swath", None),
        ("focus_top", "lapse3"),
    ):
        if lap and lap in ed:
            a, b = ed[lap]
            blend[name] = (0.5 * (a + b), max(2.0, b - a))
        else:
            blend[name] = (ed[name][0], 3.5 if name == "focus_swath" else 4.0)
    return focus_shots(rig, beats, blend, end_top)

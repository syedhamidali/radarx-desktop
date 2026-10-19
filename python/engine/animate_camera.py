"""Camera of ``xrd animate``: keyframes, easing, presets, the follow-the-storm target, camera files.

The renderer (``src/lib/utils/stormClip.ts``) turns a list of camera keys into the picture; this
module is the same maths in Python so that a camera file can be checked, described and tested
without a browser. The curve is the one of ``clipCamera.ts``: a cubic Hermite spline with monotone
(Fritsch-Butland) tangents, zero speed at both ends, no overshoot between keys.

A camera key (all angles in degrees, distances in km):

=========  =========  ==========================================================================
key        alias      meaning
=========  =========  ==========================================================================
``t``                 clip time in seconds
``azimuth``  ``theta``  where the camera is, seen from the target (180 = south of it, looking north)
``elevation`` ``phi``   angle above the horizon (90 = straight down)
``distance`` ``dist``   km from the target
``target``              ``[dx, dy, dz]``: km east, km north, km ASL height of the target, relative
                        to the point the camera follows (or ``dx`` / ``dy`` / ``dz`` separately)
``fov``                 vertical field of view in degrees (optional)
=========  =========  ==========================================================================
"""

# ruff: noqa: E501

from __future__ import annotations

import difflib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from engine.animate_plan import HOLD_S, AnimateConfigError

PRESETS = {
    "orbit": "a wide oblique view, a slow orbit, a low side view along the storm's motion, a top-down view and back",
    "wide": "a slower, wider orbit for a large system such as a tropical cyclone (see --view-km)",
    "top-down": "looking straight down, north up, slowly closing in and out",
    "window": "a camera standing on the ground beside the storm track, like one on a window sill (true scale)",
    "flight": "a flight in along a rainband, through the eyewall and into the eye of a tropical cyclone",
}

#: Ranges accepted for a camera key (the renderer clamps further; these catch typos like 2150 km).
LIMITS = {
    "azimuth": (-3600.0, 3600.0),
    "elevation": (-5.0, 89.9),
    "distance": (1.0, 3000.0),
    "dx": (-2000.0, 2000.0),
    "dy": (-2000.0, 2000.0),
    "dz": (-5.0, 100.0),
    "fov": (10.0, 120.0),
}
_KEY_ALIASES = {"theta": "azimuth", "phi": "elevation", "dist": "distance"}
_KEY_FIELDS = ("t", "azimuth", "elevation", "distance", "target", "dx", "dy", "dz", "fov")
_TOP_FIELDS = ("preset", "target_lag", "fov", "duration", "key", "keys", "observer", "view_km")
_OBSERVER_FIELDS = ("x", "y", "dist_km", "upwind_km", "side", "eye_m", "pan", "pitch_deg")


# ---------------------------------------------------------------------------
# Keyframes and the Hermite curve
# ---------------------------------------------------------------------------


@dataclass
class CamKey:
    """One camera keyframe in the renderer's own names (theta / phi / dist)."""

    t: float
    theta: float
    phi: float
    dist: float
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0
    fov: float | None = None

    def as_page(self) -> dict[str, float]:
        d = {k: float(getattr(self, k)) for k in ("t", "theta", "phi", "dist", "dx", "dy", "dz")}
        if self.fov is not None:
            d["fov"] = float(self.fov)
        return d


def hermite(ts: Sequence[float], vs: Sequence[float], t: float) -> float:
    """Cubic Hermite through ``(ts[i], vs[i])`` with monotone tangents, zero at both ends; clamped
    outside. Mirrors ``hermite`` of ``clipCamera.ts``."""
    n = len(ts)
    if n == 0:
        return 0.0
    if n == 1 or t <= ts[0]:
        return float(vs[0])
    if t >= ts[n - 1]:
        return float(vs[n - 1])
    i = 0
    while i < n - 2 and t > ts[i + 1]:
        i += 1

    def tan(k: int) -> float:
        if k <= 0 or k >= n - 1:
            return 0.0
        h0 = ts[k] - ts[k - 1]
        h1 = ts[k + 1] - ts[k]
        d0 = (vs[k] - vs[k - 1]) / h0
        d1 = (vs[k + 1] - vs[k]) / h1
        if d0 * d1 <= 0:
            return 0.0
        w1 = 2 * h1 + h0
        w2 = h1 + 2 * h0
        return (w1 + w2) / (w1 / d0 + w2 / d1)

    h = ts[i + 1] - ts[i]
    u = (t - ts[i]) / h
    u2 = u * u
    u3 = u2 * u
    return (
        (2 * u3 - 3 * u2 + 1) * vs[i]
        + (u3 - 2 * u2 + u) * h * tan(i)
        + (-2 * u3 + 3 * u2) * vs[i + 1]
        + (u3 - u2) * h * tan(i + 1)
    )


_CHANNELS = ("theta", "phi", "dist", "dx", "dy", "dz")


def camera_at(keys: Sequence[CamKey], t: float) -> CamKey:
    """The camera at clip time ``t``: every channel follows its own Hermite curve."""
    ts = [k.t for k in keys]
    vals = {c: hermite(ts, [getattr(k, c) for k in keys], t) for c in _CHANNELS}
    fov = None
    if any(k.fov is not None for k in keys):
        fov = hermite(ts, [k.fov if k.fov is not None else 45.0 for k in keys], t)
    return CamKey(t, fov=fov, **vals)


def near_to(v: float, ref: float) -> float:
    """``v`` plus a multiple of 360 degrees, as close as possible to ``ref``."""
    return v + 360.0 * round((ref - v) / 360.0)


def storm_at(track: Sequence[tuple[float, float, float]], t: float) -> tuple[float, float]:
    """Storm centre (km) at data time ``t`` (s) from ``(t, x, y)`` positions: Hermite with the end
    velocities, so the centre keeps moving at the first and the last observation. Mirrors ``stormAt``."""
    ts = [p[0] for p in track]

    def pos(vs: list[float]) -> float:
        n = len(vs)
        if n == 1:
            return vs[0]
        v0 = (vs[1] - vs[0]) / (ts[1] - ts[0])
        v1 = (vs[-1] - vs[-2]) / (ts[-1] - ts[-2])
        if t <= ts[0]:
            return vs[0] + v0 * (t - ts[0])
        if t >= ts[-1]:
            return vs[-1] + v1 * (t - ts[-1])
        i = 0
        while i < n - 2 and t > ts[i + 1]:
            i += 1

        def tan(k: int) -> float:
            if k == 0:
                return v0
            if k == n - 1:
                return v1
            return (vs[k + 1] - vs[k - 1]) / (ts[k + 1] - ts[k - 1])

        h = ts[i + 1] - ts[i]
        u = (t - ts[i]) / h
        u2 = u * u
        u3 = u2 * u
        return (
            (2 * u3 - 3 * u2 + 1) * vs[i]
            + (u3 - 2 * u2 + u) * h * tan(i)
            + (-2 * u3 + 3 * u2) * vs[i + 1]
            + (u3 - u2) * h * tan(i + 1)
        )

    return pos([p[1] for p in track]), pos([p[2] for p in track])


def track_frame(
    track_xy: Sequence[tuple[float, float]], base_half_km: float
) -> tuple[tuple[float, float], float, float]:
    """``(anchor, crop half width, track extent)``: the fixed centre of the shown box (the middle of
    the track) and its half width (the base half width plus half the track's extent)."""
    xs = [p[0] for p in track_xy]
    ys = [p[1] for p in track_xy]
    extent = max(max(xs) - min(xs), max(ys) - min(ys))
    anchor = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    return anchor, base_half_km + extent / 2, extent


def lag_target(
    anchor: tuple[float, float], storm: tuple[float, float], lag: float
) -> tuple[float, float]:
    """The point the camera looks at: the anchor plus the fraction ``lag`` of the storm's displacement
    from it (0 = fixed on the middle of the track, 1 = locked on the storm)."""
    return anchor[0] + lag * (storm[0] - anchor[0]), anchor[1] + lag * (storm[1] - anchor[1])


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def storm_choreography(
    duration: float = 36.0, heading: float = 55.0, alt_km: float = 4.0
) -> list[CamKey]:
    """The default orbit for one thunderstorm. Mirrors ``stormChoreography`` of ``clipCamera.ts``."""
    f = duration / 36.0
    behind = (heading + 180.0) % 360.0
    low = near_to(behind - 35.0, 505.0 + 60.0)
    top = near_to(180.0, low)
    end = near_to(205.0, top)

    def k(t: float, theta: float, phi: float, dist: float, dz: float) -> CamKey:
        return CamKey(t * f, theta, phi, dist, 0.0, 0.0, dz)

    return [
        k(0, 205, 30, 215, alt_km),
        k(5, 215, 27, 165, alt_km),
        k(12, 365, 22, 118, alt_km),
        k(19, 505, 18, 100, alt_km),
        k(24, low, 6, 128, alt_km * 1.6),
        k(29, low - 8, 12, 118, alt_km * 1.2),
        k(32.5, top, 86, 152, 0),
        k(36, end, 30, 215, alt_km),
    ]


def wide_choreography(
    duration: float = 48.0, view_km: float = 330.0, alt_km: float = 3.0
) -> list[CamKey]:
    """A wide, slow orbit for a system a few hundred km across. Mirrors ``cycloneChoreography``."""
    f = duration / 36.0

    def k(t: float, theta: float, phi: float, dist: float, dz: float) -> CamKey:
        return CamKey(t * f, theta, phi, dist, 0.0, 0.0, dz)

    return [
        k(0, 200, 30, view_km * 1.15, alt_km),
        k(8, 235, 24, view_km * 0.95, alt_km),
        k(17, 305, 20, view_km * 0.72, alt_km * 1.5),
        k(25, 365, 34, view_km * 0.85, alt_km),
        k(31, 400, 84, view_km * 1.2, 0),
        k(36, 380, 60, view_km * 1.3, alt_km),
    ]


def topdown_choreography(duration: float = 36.0, dist_km: float = 150.0) -> list[CamKey]:
    """Straight down with north up: a slow close-in on the storm and back out."""
    return [
        CamKey(0.0, 180, 85, dist_km * 1.25),
        CamKey(duration * 0.5, 180, 88, dist_km * 0.8),
        CamKey(duration, 180, 85, dist_km * 1.1),
    ]


@dataclass
class CameraSpec:
    """What the user asked for, before the storm's heading and the clip length are known."""

    preset: str | None = None  # a name of PRESETS
    keys: list[CamKey] | None = None  # from a camera file (the orbit camera)
    target_lag: float | None = None
    fov: float | None = None
    duration: float | None = None
    view_km: float | None = None
    observer: dict[str, Any] = field(default_factory=dict)
    source: str = "the default orbit"

    @property
    def mode(self) -> str:
        if self.keys is not None:
            return "orbit"
        return {"window": "observer", "flight": "flight"}.get(self.preset or "orbit", "orbit")


@dataclass
class CameraPlan:
    """A resolved camera, ready to hand to the renderer."""

    mode: str  # "orbit" | "observer" | "flight"
    source: str
    keys: list[CamKey] | None
    target_lag: float
    fov: float | None
    observer: dict[str, Any]
    vexag: float | None  # None: the renderer's default for the mode
    duration: float

    def page_settings(self) -> dict[str, Any]:
        s: dict[str, Any] = {"camera": self.mode, "targetLag": self.target_lag}
        if self.keys is not None:
            s["keys"] = [k.as_page() for k in self.keys]
        if self.observer:
            s["observer"] = self.observer
        if self.vexag is not None:
            s["vexag"] = self.vexag
        return s


def _suggest(name: str, known: Sequence[str]) -> str:
    close = difflib.get_close_matches(name, [*known, *_KEY_ALIASES], n=1, cutoff=0.6)
    return f' (did you mean "{close[0]}"?)' if close else ""


def check_keys(table: dict[str, Any], allowed: Sequence[str], where: str) -> None:
    """Refuse an unknown key with the nearest known one: ``unknown key "azimut" in keyframe 2 ...``."""
    for key in table:
        if key not in allowed and key not in _KEY_ALIASES:
            raise AnimateConfigError(
                f'unknown key "{key}" in {where}{_suggest(key, allowed)}. '
                f"Known keys: {', '.join(allowed)}"
            )


def _number(value: Any, label: str, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnimateConfigError(f"{label} in {where} must be a number, got {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise AnimateConfigError(f"{label} in {where} must be a finite number")
    return v


def parse_key(raw: Any, index: int) -> CamKey:
    """One ``[[key]]`` table of a camera file as a :class:`CamKey`, with clear errors."""
    where = f"keyframe {index + 1}"
    if not isinstance(raw, dict):
        raise AnimateConfigError(f"{where} must be a table with t, azimuth, elevation, distance")
    check_keys(raw, _KEY_FIELDS, where)
    d: dict[str, Any] = {}
    for k, v in raw.items():
        name = _KEY_ALIASES.get(k, k)
        if name in d:
            raise AnimateConfigError(f'{where} gives "{name}" twice (also as its alias)')
        d[name] = v
    for need in ("t", "azimuth", "elevation", "distance"):
        if need not in d:
            raise AnimateConfigError(
                f'{where} has no "{need}" (every keyframe needs t, azimuth, elevation and distance)'
            )
    vals = {n: _number(d[n], f'"{n}"', where) for n in ("t", "azimuth", "elevation", "distance")}
    tx = ty = tz = 0.0
    if "target" in d:
        tgt = d["target"]
        if not (isinstance(tgt, (list, tuple)) and len(tgt) == 3):
            raise AnimateConfigError(f'"target" in {where} must be [dx, dy, dz] (km), got {tgt!r}')
        tx, ty, tz = (_number(v, '"target"', where) for v in tgt)
    for n, name in (("dx", "dx"), ("dy", "dy"), ("dz", "dz")):
        if name in d:
            v = _number(d[name], f'"{name}"', where)
            if "target" in d:
                raise AnimateConfigError(f'{where} gives both "target" and "{name}"')
            if n == "dx":
                tx = v
            elif n == "dy":
                ty = v
            else:
                tz = v
    fov = _number(d["fov"], '"fov"', where) if "fov" in d else None
    checks = {
        "azimuth": vals["azimuth"],
        "elevation": vals["elevation"],
        "distance": vals["distance"],
        "dx": tx,
        "dy": ty,
        "dz": tz,
    }
    if fov is not None:
        checks["fov"] = fov
    for name, v in checks.items():
        lo, hi = LIMITS[name]
        if not lo <= v <= hi:
            raise AnimateConfigError(f'"{name}" = {v:g} in {where} is outside {lo:g} to {hi:g}')
    if vals["t"] < 0:
        raise AnimateConfigError(f'"t" in {where} must not be negative (got {vals["t"]:g})')
    return CamKey(vals["t"], vals["azimuth"], vals["elevation"], vals["distance"], tx, ty, tz, fov)


def parse_camera_table(data: dict[str, Any], source: str = "a camera file") -> CameraSpec:
    """The content of a camera file (already read from TOML or JSON) as a :class:`CameraSpec`."""
    check_keys(data, _TOP_FIELDS, source)
    raw_keys = data.get("key", data.get("keys"))
    spec = CameraSpec(source=source)
    preset = data.get("preset")
    if preset is not None:
        spec.preset = check_preset(str(preset))
    if raw_keys is not None:
        if not isinstance(raw_keys, list) or len(raw_keys) < 2:
            raise AnimateConfigError(
                f"{source}: give at least two [[key]] tables (a camera that does not move needs "
                "two identical keys)"
            )
        keys = [parse_key(k, i) for i, k in enumerate(raw_keys)]
        for a, b in pairwise(keys):
            if b.t <= a.t:
                raise AnimateConfigError(
                    f"keyframe times must increase: t = {b.t:g} follows t = {a.t:g} "
                    "(sort the [[key]] tables by t)"
                )
        spec.keys = keys
        spec.preset = None if spec.preset in (None, "orbit", "wide", "top-down") else spec.preset
    for name in ("target_lag", "fov", "duration", "view_km"):
        if name in data:
            setattr(spec, name, _number(data[name], f'"{name}"', source))
    if spec.target_lag is not None and not 0.0 <= spec.target_lag <= 1.0:
        raise AnimateConfigError(f'"target_lag" in {source} must be between 0 and 1')
    if spec.fov is not None and not LIMITS["fov"][0] <= spec.fov <= LIMITS["fov"][1]:
        raise AnimateConfigError(f'"fov" in {source} must be between 10 and 120 degrees')
    if spec.duration is not None and spec.duration <= 0:
        raise AnimateConfigError(f'"duration" in {source} must be positive')
    obs = data.get("observer") or {}
    if obs:
        if not isinstance(obs, dict):
            raise AnimateConfigError(f'"observer" in {source} must be a table')
        check_keys(obs, _OBSERVER_FIELDS, f"[observer] of {source}")
        spec.observer = observer_settings(obs)
    return spec


def observer_settings(obs: dict[str, Any]) -> dict[str, Any]:
    """The ``observer`` settings of the page from snake_case names (km / m / degrees)."""
    mapping = {
        "x": "x",
        "y": "y",
        "dist_km": "distKm",
        "upwind_km": "upwindKm",
        "eye_m": "eyeM",
        "pan": "pan",
        "pitch_deg": "pitchDeg",
        "side": "side",
    }
    out: dict[str, Any] = {}
    for k, v in obs.items():
        if k == "side":
            if v not in ("left", "right"):
                raise AnimateConfigError(f'"side" must be "left" or "right", got {v!r}')
            out["side"] = v
        else:
            out[mapping[k]] = _number(v, f'"{k}"', "[observer]")
    return out


def check_preset(name: str) -> str:
    if name not in PRESETS:
        close = difflib.get_close_matches(name, list(PRESETS), n=1, cutoff=0.5)
        hint = f' Did you mean "{close[0]}"?' if close else ""
        raise AnimateConfigError(
            f'unknown camera preset "{name}".{hint} Presets: {", ".join(PRESETS)}; or give a '
            "camera file (.toml / .json)"
        )
    return name


def read_table(path: Path) -> dict[str, Any]:
    """Read a TOML or JSON file into a dict, with a clear error."""
    import json

    if not path.is_file():
        raise AnimateConfigError(f"file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        elif path.suffix.lower() == ".toml":
            try:
                import tomllib
            except ImportError as exc:  # Python 3.10
                raise AnimateConfigError(
                    f"{path.name}: reading .toml needs Python 3.11 or newer; use a .json file"
                ) from exc
            data = tomllib.loads(text)
        else:
            raise AnimateConfigError(f"{path.name}: use a .toml or .json file")
    except (OSError, ValueError) as exc:
        if isinstance(exc, AnimateConfigError):
            raise
        raise AnimateConfigError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise AnimateConfigError(f"{path.name} must hold key = value pairs")
    return data


def load_camera(arg: str | None, view_km: float | None = None) -> CameraSpec:
    """``--camera``: a preset name, or a camera file (.toml / .json). None = the default orbit."""
    if not arg:
        return CameraSpec(preset="orbit", view_km=view_km, source="the default orbit")
    if arg.lower().endswith((".toml", ".json")) or "/" in arg or "\\" in arg:
        path = Path(arg).expanduser()
        spec = parse_camera_table(read_table(path), path.name)
        spec.source = f"camera file {path.name}"
        if spec.preset is None and spec.keys is None:
            spec.preset = "orbit"
        if view_km is not None and spec.view_km is None:
            spec.view_km = view_km
        return spec
    return CameraSpec(preset=check_preset(arg), view_km=view_km, source=f"the {arg} preset")


def resolve_camera(
    spec: CameraSpec,
    duration: float | None,
    heading_deg: float,
    target_lag: float | None = None,
    fov: float | None = None,
    observer: dict[str, Any] | None = None,
    default_duration: float = 36.0,
) -> CameraPlan:
    """Fix the camera for a clip of ``duration`` s and a storm heading (degrees clockwise from north).

    Priority for the duration: ``duration`` (the option), then the camera file's, then the last key's
    time, then ``default_duration``. The follow factor and field of view: the option, then the file.
    """
    obs = {**spec.observer, **(observer or {})}
    lag = target_lag if target_lag is not None else spec.target_lag
    fov_deg = fov if fov is not None else spec.fov
    if spec.keys is not None:
        dur = duration or spec.duration or spec.keys[-1].t
        keys = [CamKey(**{**k.__dict__}) for k in spec.keys]
        if fov_deg is not None:
            for k in keys:
                if k.fov is None:
                    k.fov = fov_deg
        mode, vexag = "orbit", None
        lag = 0.3 if lag is None else lag
    else:
        preset = spec.preset or "orbit"
        dur = duration or spec.duration or default_duration
        keys = None
        vexag = None
        if preset == "orbit":
            keys = storm_choreography(dur, heading_deg, 4.0)
            mode, lag = "orbit", 0.3 if lag is None else lag
        elif preset == "wide":
            keys = wide_choreography(dur, spec.view_km or 330.0)
            mode, lag = "orbit", 0.3 if lag is None else lag
        elif preset == "top-down":
            keys = topdown_choreography(dur)
            mode, lag = "orbit", 0.5 if lag is None else lag
        elif preset == "window":
            mode, lag, vexag = "observer", 0.3 if lag is None else lag, 1.0
        else:  # flight
            mode, lag, vexag = "flight", 0.3 if lag is None else lag, 1.0
        if keys is not None and fov_deg is not None:
            for k in keys:
                k.fov = fov_deg
    return CameraPlan(mode, spec.source, keys, lag, fov_deg, obs, vexag, float(dur))


def describe_path(keys: Sequence[CamKey], duration: float, fps: float = 30.0) -> dict[str, Any]:
    """Numbers for the report: how far the camera turns and moves, and its fastest turn."""
    n = max(2, round(duration * fps))
    pts = [camera_at(keys, i / fps) for i in range(n)]
    az = [p.theta for p in pts]
    swept = sum(abs(b - a) for a, b in pairwise(az))
    turn = max((abs(b - a) * fps for a, b in pairwise(az)), default=0.0)
    return {
        "keys": len(keys),
        "azimuth_swept_deg": swept,
        "max_turn_deg_s": turn,
        "elevation_deg": [min(p.phi for p in pts), max(p.phi for p in pts)],
        "distance_km": [min(p.dist for p in pts), max(p.dist for p in pts)],
        "ends_after_clip": keys[-1].t > duration + 1e-9,
        "holds_last_pose_s": max(0.0, duration - keys[-1].t),
    }


def check_duration(duration: float, hold: float = HOLD_S) -> None:
    if duration <= 2 * hold + 0.5:
        raise AnimateConfigError(
            f"--duration {duration:g} s is too short: the clip holds the first and last volume "
            f"{hold:g} s each, so it must be longer than {2 * hold + 0.5:g} s"
        )

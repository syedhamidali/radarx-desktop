"""Clip clock and camera: how clip seconds map to data seconds, and where the listener is.

Convention: "data time" is seconds since the bundle's window start (manifest). The clip is
``clip_duration_s`` long; ``speedup`` is data seconds per clip second (a number, or a list of
keyframes ``[{"t_clip_s", "speedup"}]``, linear in between).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def parse_iso(s: str) -> datetime:
    d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class Timeline:
    clip_duration_s: float
    fps: float
    speedup: float | None  # constant speedup, or None when keyframed
    data_offset_s: float  # data time (s since window start) at clip time 0
    keys_t: np.ndarray | None = None
    keys_s: np.ndarray | None = None
    data_start: str | None = None
    data_end: str | None = None
    _grid_t: np.ndarray | None = None
    _grid_d: np.ndarray | None = None

    @classmethod
    def from_dict(cls, d: dict, window_start: str | None = None) -> Timeline:
        dur = float(d.get("clip_duration_s", 120.0))
        fps = float(d.get("fps", 30.0))
        ds, de = d.get("data_start"), d.get("data_end")
        off = 0.0
        if ds and window_start:
            off = (parse_iso(ds) - parse_iso(window_start)).total_seconds()
        sp = d.get("speedup")
        if sp is None and ds and de:
            sp = (parse_iso(de) - parse_iso(ds)).total_seconds() / dur
        if isinstance(sp, list | tuple):
            kt = np.array([float(k["t_clip_s"]) for k in sp])
            ks = np.array([float(k["speedup"]) for k in sp])
            tl = cls(dur, fps, None, off, kt, ks, ds, de)
            tg = np.arange(0.0, dur + 0.01, 0.005)
            sg = np.interp(tg, kt, ks)
            dg = np.concatenate([[0.0], np.cumsum(0.5 * (sg[1:] + sg[:-1]) * np.diff(tg))])
            tl._grid_t, tl._grid_d = tg, dg + off
            return tl
        if sp is None or float(sp) <= 0:
            raise ValueError("timeline has no usable speedup")
        return cls(dur, fps, float(sp), off, None, None, ds, de)

    @classmethod
    def constant(cls, clip_duration_s: float, speedup: float, data_offset_s: float = 0.0, fps=30.0):
        return cls(clip_duration_s, fps, float(speedup), data_offset_s)

    def clip_to_data(self, t_clip):
        """Data time (s since window start) at clip time(s)."""
        t = np.asarray(t_clip, float)
        if self.speedup is not None:
            return self.data_offset_s + t * self.speedup
        return np.interp(t, self._grid_t, self._grid_d)

    def data_to_clip(self, t_data):
        """Clip time (s) at data time(s) (may be negative or beyond the clip)."""
        d = np.asarray(t_data, float)
        if self.speedup is not None:
            return (d - self.data_offset_s) / self.speedup
        out = np.interp(d, self._grid_d, self._grid_t)
        # extrapolate with the end speedups
        lo = d < self._grid_d[0]
        hi = d > self._grid_d[-1]
        out = np.where(lo, self._grid_t[0] + (d - self._grid_d[0]) / self.keys_s[0], out)
        return np.where(hi, self._grid_t[-1] + (d - self._grid_d[-1]) / self.keys_s[-1], out)

    def speedup_at(self, t_clip):
        t = np.asarray(t_clip, float)
        if self.speedup is not None:
            return np.full_like(t, self.speedup)
        return np.interp(t, self.keys_t, self.keys_s)


def load_timeline(src, window_start: str | None = None) -> Timeline:
    if isinstance(src, Timeline):
        return src
    if isinstance(src, str | Path):
        src = json.loads(Path(src).read_text())
    return Timeline.from_dict(src, window_start)


class CameraPath:
    """Camera (= listener) keyframes interpolated in time; heading unwrapped."""

    def __init__(self, keys: list[dict]):
        keys = sorted(keys, key=lambda k: float(k["t_clip_s"]))
        self.t = np.array([float(k["t_clip_s"]) for k in keys])
        self.lat = np.array([float(k["lat"]) for k in keys])
        self.lon = np.array([float(k["lon"]) for k in keys])
        self.alt = np.array([float(k.get("alt_m", 0.0)) for k in keys])
        self.heading = np.degrees(np.unwrap(np.radians([float(k.get("heading_deg", 0.0)) for k in keys])))
        self.pitch = np.array([float(k.get("pitch_deg", 0.0)) for k in keys])

    def at(self, t):
        t = np.asarray(t, float)
        return {
            "lat": np.interp(t, self.t, self.lat),
            "lon": np.interp(t, self.t, self.lon),
            "alt_m": np.interp(t, self.t, self.alt),
            "heading_deg": np.interp(t, self.t, self.heading) % 360.0,
            "pitch_deg": np.interp(t, self.t, self.pitch),
        }


def load_camera(src) -> CameraPath:
    if isinstance(src, CameraPath):
        return src
    if isinstance(src, str | Path):
        src = json.loads(Path(src).read_text())
    if isinstance(src, dict):
        src = src.get("path") or src.get("keys") or src.get("camera")
    return CameraPath(src)

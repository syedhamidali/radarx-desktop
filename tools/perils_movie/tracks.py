"""Tracking: line / storm motion from reflectivity, vortex tracks, and the comparison with the tornado survey.

* ``estimate_motion``: displacement of the echo pattern between two maps by the maximum of their
  cross-correlation (FFT, zero padded, sub-pixel by a parabola); a translation of the whole pattern,
  not of one cell. Reported with the correlation peak and the lag search limit.
* ``link_vortices``: nearest-neighbour linking of the vortex candidates of consecutive steps (``rotation.py``).
* ``compare_with_survey``: distance between the strongest vortex candidate near the surveyed track and
  the surveyed position (interpolated, a straight line) at every step, and the time shift of the
  surveyed track that minimises the mean distance: the offsets in km and s asked for in the validation.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def estimate_motion(
    a: np.ndarray,
    b: np.ndarray,
    dt_s: float,
    cell_km: float,
    max_lag_km: float = 30.0,
    min_dbz: float = 20.0,
) -> dict[str, Any]:
    """Motion (m/s east, north) of the pattern from map ``a`` at t to map ``b`` at t + ``dt_s`` (dBZ maps)."""
    fa = np.where(np.isfinite(a) & (a >= min_dbz), a - min_dbz, 0.0)
    fb = np.where(np.isfinite(b) & (b >= min_dbz), b - min_dbz, 0.0)
    fa = fa - fa.mean()
    fb = fb - fb.mean()
    ny, nx = fa.shape
    sz = (2 * ny, 2 * nx)
    F = np.fft.rfft2(fa, sz)
    G = np.fft.rfft2(fb, sz)
    cc = np.fft.irfft2(np.conj(F) * G, sz)  # cc[dy, dx]: shift of b relative to a
    cc = np.fft.fftshift(cc)
    cy, cx = sz[0] // 2, sz[1] // 2
    lag = round(max_lag_km / cell_km)
    sub = cc[cy - lag : cy + lag + 1, cx - lag : cx + lag + 1]
    j, i = np.unravel_index(int(np.argmax(sub)), sub.shape)

    # parabolic refinement
    def refine(c0, c1, c2):
        d = c0 - 2 * c1 + c2
        return 0.0 if abs(d) < 1e-12 else 0.5 * (c0 - c2) / d

    dy = j - lag
    dx = i - lag
    ry = refine(sub[j - 1, i], sub[j, i], sub[j + 1, i]) if 0 < j < sub.shape[0] - 1 else 0.0
    rx = refine(sub[j, i - 1], sub[j, i], sub[j, i + 1]) if 0 < i < sub.shape[1] - 1 else 0.0
    shift_x = (dx + rx) * cell_km * 1000.0
    shift_y = (dy + ry) * cell_km * 1000.0
    norm = math.sqrt(float((fa**2).sum() * (fb**2).sum()))
    return {
        "u_ms": shift_x / dt_s,
        "v_ms": shift_y / dt_s,
        "speed_ms": math.hypot(shift_x, shift_y) / dt_s,
        "from_deg": (math.degrees(math.atan2(-shift_x, -shift_y)) + 360.0) % 360.0,
        "toward_deg": (math.degrees(math.atan2(shift_x, shift_y)) + 360.0) % 360.0,
        "correlation": float(sub[j, i] / norm) if norm > 0 else None,
        "dt_s": dt_s,
        "max_lag_km": max_lag_km,
        "method": "FFT cross-correlation of the column-maximum reflectivity above min_dbz, parabolic sub-pixel peak",
    }


def link_vortices(
    per_step: list[list[dict[str, Any]]], times_s: list[float], max_km: float = 5.0
) -> list[dict[str, Any]]:
    """Tracks (lists of detections) by nearest-neighbour linking of strongest-first vortex candidates."""
    tracks: list[dict[str, Any]] = []
    open_tracks: list[dict[str, Any]] = []
    for k, dets in enumerate(per_step):
        used = set()
        for tr in open_tracks:
            last = tr["points"][-1]
            best, bd = None, max_km
            for j, d in enumerate(dets):
                if j in used:
                    continue
                dist = math.hypot(d["x_km"] - last["x_km"], d["y_km"] - last["y_km"])
                if dist < bd:
                    best, bd = j, dist
            if best is not None:
                used.add(best)
                tr["points"].append({**dets[best], "t_s": times_s[k], "step": k})
            else:
                tr["closed"] = True
        open_tracks = [t for t in open_tracks if not t.get("closed")]
        for j, d in enumerate(dets):
            if j in used:
                continue
            tr = {"id": len(tracks) + 1, "points": [{**d, "t_s": times_s[k], "step": k}]}
            tracks.append(tr)
            open_tracks.append(tr)
    for tr in tracks:
        pk = [p["peak_as"] for p in tr["points"]]
        tr["n_steps"] = len(tr["points"])
        tr["peak_as_max"] = max(pk)
        tr["duration_s"] = tr["points"][-1]["t_s"] - tr["points"][0]["t_s"]
    return tracks


def compare_with_survey(
    per_step: list[list[dict[str, Any]]],
    times_s: list[float],
    survey_xy_km: list[tuple[float, float, float]],
    search_km: float = 12.0,
    max_shift_s: float = 600.0,
    min_peak_as: float = 0.012,
) -> dict[str, Any]:
    """Offsets between the detected vortex candidates and the surveyed tornado.

    ``survey_xy_km`` = [(t_s, x_km, y_km), ...] of the surveyed (interpolated) track in the grid frame.
    For each step with a surveyed position, the candidate within ``search_km`` with the largest azimuthal
    shear (>= ``min_peak_as``) is compared; ``time_shift_s`` is the shift of the survey time (in 30 s
    steps up to ``max_shift_s``) that minimises the mean distance over the steps that have a candidate:
    a positive shift means the radar vortex is where the survey puts the tornado that many seconds later.
    """
    st = np.array([p[0] for p in survey_xy_km])
    sx = np.array([p[1] for p in survey_xy_km])
    sy = np.array([p[2] for p in survey_xy_km])

    def survey_at(t):
        if t < st[0] or t > st[-1]:
            return None
        return float(np.interp(t, st, sx)), float(np.interp(t, st, sy))

    rows = []
    for dets, t in zip(per_step, times_s, strict=True):
        s = survey_at(t)
        if s is None:
            continue
        cands = [
            d
            for d in dets
            if d["peak_as"] >= min_peak_as
            and math.hypot(d["x_km"] - s[0], d["y_km"] - s[1]) <= search_km
        ]
        if not cands:
            rows.append({"t_s": t, "survey_xy_km": s, "detected": False})
            continue
        d = max(cands, key=lambda c: c["peak_as"])
        rows.append(
            {
                "t_s": t,
                "survey_xy_km": [round(s[0], 2), round(s[1], 2)],
                "detected": True,
                "vortex_xy_km": [round(d["x_km"], 2), round(d["y_km"], 2)],
                "distance_km": round(math.hypot(d["x_km"] - s[0], d["y_km"] - s[1]), 2),
                "peak_as": round(d["peak_as"], 4),
                "vrot_ms": d.get("vrot_ms"),
                "radar": d.get("radar"),
                "pos_unc_km": d.get("pos_unc_km"),
            }
        )
    det = [r for r in rows if r["detected"]]
    out: dict[str, Any] = {
        "n_steps_with_survey_position": len(rows),
        "n_steps_detected": len(det),
        "search_km": search_km,
        "min_peak_as": min_peak_as,
        "rows": rows,
        "note": "the survey track is a straight line between begin and end (INTERPOLATED); the real track curves",
    }
    if det:
        d = np.array([r["distance_km"] for r in det])
        out["distance_km_median"] = float(np.median(d))
        out["distance_km_min"] = float(d.min())
        out["distance_km_mean"] = float(d.mean())
        best_shift, best_val = 0.0, float("inf")
        for sh in np.arange(-max_shift_s, max_shift_s + 1, 30.0):
            vals = []
            for r in det:
                s = survey_at(r["t_s"] + sh)
                if s is not None:
                    vals.append(
                        math.hypot(r["vortex_xy_km"][0] - s[0], r["vortex_xy_km"][1] - s[1])
                    )
            if len(vals) >= max(2, len(det) // 2):
                m = float(np.mean(vals))
                if m < best_val:
                    best_shift, best_val = float(sh), m
        out["time_shift_s"] = best_shift
        out["mean_distance_at_best_shift_km"] = best_val if best_val < float("inf") else None
    return out

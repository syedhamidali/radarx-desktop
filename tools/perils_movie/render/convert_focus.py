"""The low-level radar products of the tornado-focus sequence (docs/PERILS_MOVIE.md, "Tornado focus").

For every step of the bundle, from the merged volume's lowest levels:

* ``vsr_k.bin`` - the **storm-relative radial velocity** at about 0.5 km above sea level (OBSERVED radial velocity of the
  best radar of each voxel, ``vel`` / ``vel_src``, minus the component of the echo-pattern motion along that radar's beam;
  8-bit codes of -VSR_LIM..+VSR_LIM m/s, 0 = no data). Red is moving away from the radar, blue toward it, as in any
  radar velocity display; a cyclonic couplet is a red / blue pair across the beam.
* ``shear_k.bin`` - the low-level **azimuthal shear** ``rotation_ll`` (s-1, cyclonic > 0; 0 .. SHEAR_LIM as 8-bit codes).
* a small time series around the surveyed tornado position (azimuthal shear and Vrot maxima within ``RADIUS_KM`` of
  it, where they are, which radar saw them), with the observed rotation candidates' offset from the surveyed position.

Pure numpy; tested in python/tests/test_perils_focus.py.
"""

from __future__ import annotations

import math

import numpy as np

VSR_LIM = 35.0  # m/s: the velocity colour scale of the inset
SHEAR_LIM = 0.04  # s-1: the shear colour scale of the inset
RADIUS_KM = 3.0  # search radius around the surveyed position for the rotation maximum
LEVELS = (2, 3, 4)  # 0.5, 0.75 and 1.0 km above sea level: the lowest level with a velocity is used


def encode_signed(v: np.ndarray, lim: float) -> np.ndarray:
    """-lim..+lim -> 1..255 (128 is zero); NaN -> 0."""
    a = np.asarray(v, float)
    ok = np.isfinite(a)
    c = np.rint((np.clip(np.where(ok, a, 0.0), -lim, lim) / lim) * 127.0 + 128.0)
    return np.where(ok, np.clip(c, 1, 255), 0).astype(np.uint8)


def decode_signed(c: np.ndarray, lim: float) -> np.ndarray:
    c = np.asarray(c)
    return np.where(c == 0, np.nan, (c.astype(float) - 128.0) / 127.0 * lim)


def encode_unsigned(v: np.ndarray, lim: float) -> np.ndarray:
    """0..lim -> 1..255; negative and NaN -> 0."""
    a = np.asarray(v, float)
    ok = np.isfinite(a) & (a > 0)
    c = np.rint(np.clip(np.where(ok, a, 0.0), 0, lim) / lim * 254.0) + 1
    return np.where(ok, c, 0).astype(np.uint8)


def storm_relative_radial(
    vel: np.ndarray,
    src: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    radar_xy: list[tuple[float, float]],
    motion_ms: tuple[float, float],
) -> np.ndarray:
    """``vel`` (ny, nx) minus the beam component of ``motion_ms``; ``src`` is the 1-based radar index of each voxel (0 = none).

    The beam direction is the horizontal unit vector from the radar to the voxel (the elevation angle is small: the
    voxels are at 0.5 - 1 km and the ranges are tens of km; closer than 1.5 km to the radar nothing is returned).
    """
    X, Y = np.meshgrid(xs, ys)
    out = np.full(vel.shape, np.nan)
    for s in np.unique(src):
        if s == 0 or s > len(radar_xy):
            continue
        rx, ry = radar_xy[int(s) - 1]
        sel = (src == s) & np.isfinite(vel)
        dx, dy = X - rx, Y - ry
        r = np.hypot(dx, dy)
        sel &= r > 1.5
        out[sel] = vel[sel] - (motion_ms[0] * dx[sel] + motion_ms[1] * dy[sel]) / r[sel]
    return out


def lowest_valid(vol: np.ndarray, levels=LEVELS) -> np.ndarray:
    """The first finite value over ``levels`` of a (nz, ny, nx) array, NaN where none."""
    out = np.full(vol.shape[1:], np.nan, dtype=float)
    for k in levels:
        a = vol[k].astype(float)
        m = ~np.isfinite(out) & np.isfinite(a)
        out[m] = a[m]
    return out


def lowest_valid_pair(
    vel: np.ndarray, src: np.ndarray, levels=LEVELS
) -> tuple[np.ndarray, np.ndarray]:
    """Velocity and its source index at the lowest level that has a velocity."""
    v = np.full(vel.shape[1:], np.nan, dtype=float)
    s = np.zeros(vel.shape[1:], dtype=np.uint8)
    for k in levels:
        a = vel[k].astype(float)
        m = ~np.isfinite(v) & np.isfinite(a) & (src[k] > 0)
        v[m] = a[m]
        s[m] = src[k][m]
    return v, s


def bearing_deg(dx: float, dy: float) -> float:
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def vortex_sample(
    rot_ll: np.ndarray,
    vrot_ll: np.ndarray,
    src_ll: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    ref_xy: tuple[float, float],
    radius_km: float = RADIUS_KM,
) -> dict:
    """The rotation maximum within ``radius_km`` of the surveyed position ``ref_xy`` (km): where, how strong, which radar."""
    X, Y = np.meshgrid(xs, ys)
    d = np.hypot(X - ref_xy[0], Y - ref_xy[1])
    inside = d <= radius_km
    a = np.where(inside & np.isfinite(rot_ll), rot_ll, -np.inf)
    if not np.isfinite(a).any() or np.max(a) <= 0:
        return {
            "as": None,
            "vrot": None,
            "x": None,
            "y": None,
            "offset_km": None,
            "bearing_deg": None,
            "radar": None,
        }
    j, i = np.unravel_index(int(np.argmax(a)), a.shape)
    v = np.where(inside & np.isfinite(vrot_ll), vrot_ll, -np.inf)
    near1 = d <= 1.0
    a1 = np.where(near1 & np.isfinite(rot_ll), rot_ll, -np.inf)
    dx, dy = float(xs[i] - ref_xy[0]), float(ys[j] - ref_xy[1])
    return {
        "as": float(a[j, i]),
        "as_within_1km": float(np.max(a1)) if np.isfinite(a1).any() else None,
        "vrot": float(np.max(v)) if np.isfinite(v).any() else None,
        "x": float(xs[i]),
        "y": float(ys[j]),
        "offset_km": float(math.hypot(dx, dy)),
        "bearing_deg": bearing_deg(dx, dy),
        "radar_index": int(src_ll[j, i]) if src_ll is not None else None,
    }


def echo_context(
    cmax: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    ref_xy: tuple[float, float],
    thresholds=(40.0, 50.0),
) -> dict:
    """The column-maximum echo at the surveyed position and where the nearest strong echo (>= each threshold) is: km and bearing."""
    X, Y = np.meshgrid(xs, ys)
    d = np.hypot(X - ref_xy[0], Y - ref_xy[1])
    i0 = np.unravel_index(int(np.argmin(d)), d.shape)
    out: dict = {"cmax_at_dbz": float(cmax[i0]) if np.isfinite(cmax[i0]) else None}
    for thr in thresholds:
        sel = np.isfinite(cmax) & (cmax >= thr)
        key = f"core{int(thr)}"
        if not sel.any():
            out[f"{key}_km"], out[f"{key}_bearing_deg"] = None, None
            continue
        dd = np.where(sel, d, np.inf)
        j, i = np.unravel_index(int(np.argmin(dd)), dd.shape)
        out[f"{key}_km"] = float(dd[j, i])
        out[f"{key}_bearing_deg"] = bearing_deg(float(xs[i] - ref_xy[0]), float(ys[j] - ref_xy[1]))
    return out


def wind_context(
    path, ref_xy: tuple[float, float], radius_km: float = 5.0, level_km: float = 0.5
) -> dict | None:
    """Mean storm-relative wind (multi-Doppler retrieval, RETRIEVED; the synthesis did not fully converge) within
    ``radius_km`` of ``ref_xy`` at about ``level_km``: speed (m/s) and the bearing it blows FROM; None without the file."""
    try:
        w = np.load(path)
    except (OSError, ValueError, FileNotFoundError):
        return None
    if not {"u_sr", "v_sr", "x_km", "y_km", "z_km"} <= set(w.files):
        return None
    k = int(np.argmin(np.abs(np.asarray(w["z_km"], float) - level_km)))
    X, Y = np.meshgrid(np.asarray(w["x_km"], float), np.asarray(w["y_km"], float))
    m = np.hypot(X - ref_xy[0], Y - ref_xy[1]) <= radius_km
    u, v = np.asarray(w["u_sr"][k], float), np.asarray(w["v_sr"][k], float)
    ok = m & np.isfinite(u) & np.isfinite(v)
    if ok.sum() < 5:
        return None
    mu, mv = float(u[ok].mean()), float(v[ok].mean())
    return {
        "speed_ms": float(math.hypot(mu, mv)),
        "from_deg": (bearing_deg(mu, mv) + 180.0) % 360.0,
        "valid_fraction": float(ok.sum() / max(1, m.sum())),
    }


def focus_step(
    z,
    xs: np.ndarray,
    ys: np.ndarray,
    radar_xy: list[tuple[float, float]],
    motion_ms: tuple[float, float],
) -> dict:
    """Everything of one volume (an opened npz): the two 8-bit images and the arrays the series needs."""
    shape = (len(ys), len(xs))
    if "vel" in z.files and "vel_src" in z.files:
        vel, src = lowest_valid_pair(z["vel"], z["vel_src"])
    else:  # a bundle without the velocity source: nothing can be made storm-relative
        vel, src = np.full(shape, np.nan), np.zeros(shape, np.uint8)
    vsr = storm_relative_radial(vel, src, xs, ys, radar_xy, motion_ms)
    rot = (
        np.asarray(z["rotation_ll"], float) if "rotation_ll" in z.files else np.full(shape, np.nan)
    )
    vr = np.asarray(z["vrot_ll"], float) if "vrot_ll" in z.files else np.full(shape, np.nan)
    return {
        "vsr_codes": encode_signed(vsr, VSR_LIM),
        "shear_codes": encode_unsigned(rot, SHEAR_LIM),
        "rot": rot,
        "vrot": vr,
        "vel_src": src,
        "vsr": vsr,
        "cmax": np.asarray(z["cmax"], float)
        if "cmax" in z.files
        else np.nanmax(np.where(np.isfinite(z["dbz"]), z["dbz"], -99.0), axis=0),
    }


def nearest_index(a: np.ndarray, v: float) -> int:
    return int(np.argmin(np.abs(np.asarray(a) - v)))

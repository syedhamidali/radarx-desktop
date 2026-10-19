"""Thunder from every real LMA flash.

Physically grounded: every VHF source is a point emitter; its sound arrives at the listener
(the camera) at  t_source + r / 343 m/s  in data time, which is then mapped to clip time
(= flash clip time + r / 343 / speedup for a constant speedup). A long flash therefore starts at
its nearest part and rolls on as the farther parts arrive. Level falls as 1/r (minimum range,
level floor); the spectrum loses its top with range through the ISO 9613-1 air absorption.

Artistic: the noise carrier, the crack of a close strike, the energy proxy from the number of
sources and their relative power, the length of the tail. Synthesised, not a recording.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import lfilter

from .acoustics import SPEED_OF_SOUND_MS, air_absorption_db_per_m, binaural, db, distance_gain
from .geo import bearing_deg, enu, rel_azimuth_deg
from .timeline import CameraPath, Timeline


@dataclass
class ThunderParams:
    a1: float = 0.35  # amplitude (1.0 = full scale before the master) of a reference flash at 1 km
    r0_m: float = 300.0  # minimum range of the 1/r law
    floor_db: float = -42.0  # level floor relative to a1 (reported when engaged)
    max_range_m: float = 90_000.0  # beyond this a flash is not rendered
    env_rate: int = 1000
    n_sectors: int = 6
    rh_pct: float = 80.0  # assumed (not in the data)
    tail_s_per_km: float = 0.05
    tail_min_s: float = 0.5
    tail_max_s: float = 4.0
    crack_range_m: float = 4000.0
    # thinning: at most ``window_cap`` flashes whose first arrival is within +-``window_s``; a flash
    # more than ``mask_db`` below a louder one within +-``mask_s`` is masked
    window_s: float = 1.5
    window_cap: int = 3
    mask_db: float = 30.0
    mask_s: float = 3.0


def flash_seed(seed: int, fid: str) -> int:
    return (seed * 1_000_003 + zlib.crc32(str(fid).encode())) & 0x7FFFFFFF


def plan_flash(flash, timeline: Timeline, camera: CameraPath, duration: float, prm: ThunderParams) -> dict:
    """Timing, distances and level of one flash for the camera as listener. No audio."""
    src = flash.sources
    row = {
        "id": flash.id,
        "n_sources": int(flash.n_sources or len(src)),
        "status": "pending",
    }
    if len(src) == 0:
        row["status"] = "no_sources"
        return row
    t_i = src[:, 0]
    tf = float(timeline.data_to_clip(flash.t_start_s))
    cam = camera.at(np.clip(tf, 0.0, duration))
    e, n, u = enu(cam["lat"], cam["lon"], cam["alt_m"], src[:, 1], src[:, 2], src[:, 3])
    r = np.sqrt(e * e + n * n + u * u)
    k = int(np.argmin(r))
    arr = timeline.data_to_clip(t_i + r / SPEED_OF_SOUND_MS)
    emit = timeline.data_to_clip(t_i)
    p = src[:, 4]
    pm = np.nanmedian(p) if np.isfinite(p).any() else 0.0
    w = np.clip(10 ** ((np.nan_to_num(p, nan=pm) - pm) / 40.0), 0.4, 2.5)
    A, floored = distance_gain(r, prm.a1, prm.r0_m, prm.floor_db)
    q = (w * A) ** 2
    nf = float(np.clip((row["n_sources"] / 50.0) ** 0.25, 0.6, 1.6))
    a_flash = float(np.sqrt(q.sum() / (w**2).sum()) * nf)
    cen = flash.centroid
    if np.isfinite(cen[0]):
        ce, cn, cu = enu(cam["lat"], cam["lon"], cam["alt_m"], cen[0], cen[1], cen[2])
        r_cen = float(np.sqrt(ce * ce + cn * cn + cu * cu))
    else:
        r_cen = float(np.mean(r))
    row.update(
        t_flash_clip_s=float(tf),
        listener={"lat": float(cam["lat"]), "lon": float(cam["lon"]), "alt_m": float(cam["alt_m"]),
                  "heading_deg": float(cam["heading_deg"])},
        dist_nearest_m=float(r[k]),
        dist_farthest_m=float(r.max()),
        dist_centroid_m=r_cen,
        bearing_nearest_deg=float(bearing_deg(e[k], n[k])),
        az_rel_nearest_deg=float(rel_azimuth_deg(bearing_deg(e[k], n[k]), cam["heading_deg"])),
        first_arrival_clip_s=float(arr.min()),
        last_arrival_clip_s=float(arr.max()),
        delay_nearest_clip_s=float(arr[k] - emit[k]),
        delay_nearest_real_s=float(r[k] / SPEED_OF_SOUND_MS),
        speedup_at_flash=float(timeline.speedup_at(np.clip(tf, 0, duration))),
        peak_amp=a_flash,
        peak_dbfs_premaster=float(db(a_flash)),
        level_floor_engaged=bool(floored.mean() > 0.5),
        power_dbw_median=float(pm),
        t_light_start_data_s=float(flash.t_start_s),
        _r=r, _arr=arr, _q=q, _e=e, _n=n, _u=u, _w=w, _cam=cam,
    )
    if r[k] > prm.max_range_m:
        row["status"] = "skipped_out_of_range"
    elif arr.min() >= duration or arr.max() + prm.tail_max_s < 0:
        row["status"] = "outside_clip"
    return row


def select_flashes(rows: list[dict], prm: ThunderParams) -> dict[str, int]:
    """Thin out dense bursts: greedy by loudness. Sets row["status"] to "rendered" or
    "skipped_density" / "skipped_masked" and returns the count per status."""
    cand = [r for r in rows if r["status"] == "pending"]
    cand.sort(key=lambda r: -r["peak_amp"])
    kept: list[dict] = []
    for r in cand:
        a = r["first_arrival_clip_s"]
        near = [k for k in kept if abs(k["first_arrival_clip_s"] - a) <= prm.window_s]
        if len(near) >= prm.window_cap:
            r["status"] = "skipped_density"
            continue
        louder = [
            k for k in kept
            if abs(k["first_arrival_clip_s"] - a) <= prm.mask_s
            and db(k["peak_amp"]) - db(r["peak_amp"]) > prm.mask_db
        ]
        if louder:
            r["status"] = "skipped_masked"
            continue
        r["status"] = "rendered"
        kept.append(r)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


def _rumble_shape(f: np.ndarray) -> np.ndarray:
    """Source spectrum of the carrier: -6 dB/octave above 200 Hz, nothing below ~25 Hz."""
    return (1.0 / np.sqrt(1.0 + (f / 200.0) ** 2)) * (f**2 / (f**2 + 25.0**2))


def _shaped_noise(rng, n: int, sr: int, r_m: float, temp_c: float, p_hpa: float, rh: float, base: str = "rumble"):
    """Unit-RMS noise with the source shape and the air absorption of ``r_m`` metres."""
    x = rng.standard_normal(n)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    alpha = air_absorption_db_per_m(np.maximum(f, 1.0), temp_c, rh, p_hpa)
    H = 10 ** (-alpha * r_m / 20.0)
    if base == "rumble":
        H = H * _rumble_shape(f)
    else:  # crack: flat above 500 Hz
        H = H * (f**2 / (f**2 + 500.0**2))
    y = np.fft.irfft(X * H, n)
    s = np.sqrt(np.mean(y * y))
    return y / s if s > 0 else y


def render_flash(row: dict, sr: int, seed: int, camera: CameraPath, prm: ThunderParams, air: tuple[float, float]):
    """Stereo audio of one planned flash: (start_sample, (2, L) array)."""
    rng = np.random.default_rng(flash_seed(seed, row["id"]))
    r, arr, q = row["_r"], row["_arr"], row["_q"]
    temp_c, p_hpa = air
    rmin_km = row["dist_nearest_m"] / 1000.0
    tau = float(np.clip(prm.tail_min_s + prm.tail_s_per_km * rmin_km, prm.tail_min_s, prm.tail_max_s))
    t_a = float(arr.min()) - 0.05
    span = float(arr.max() - arr.min()) + 0.1 + 6.0 * tau
    rate = prm.env_rate
    nb = int(np.ceil(span * rate)) + 1
    L = int(np.ceil(span * sr)) + 1
    bearing = bearing_deg(row["_e"], row["_n"])
    sec = np.minimum((bearing / (360.0 / prm.n_sectors)).astype(int), prm.n_sectors - 1)
    bin_idx = np.clip(np.round((arr - t_a) * rate).astype(int), 0, nb - 1)
    alpha = np.exp(-1.0 / (rate * tau))
    S: dict[int, np.ndarray] = {}
    info: dict[int, tuple[float, float, float]] = {}
    for s in range(prm.n_sectors):
        m = sec == s
        if not m.any():
            continue
        qs = q[m]
        r_eff = float((qs * r[m]).sum() / qs.sum())
        sig = (0.04 + 0.01 * np.sqrt(r_eff / 1000.0)) * rate
        h = np.bincount(bin_idx[m], weights=qs, minlength=nb).astype(float)
        h = gaussian_filter1d(h, sig, mode="constant")
        h = lfilter([1 - alpha], [1, -alpha], h)  # the ringing tail, on power
        S[s] = h
        ux, uy = np.sin(np.radians(bearing[m])), np.cos(np.radians(bearing[m]))
        mean_b = float(np.degrees(np.arctan2((qs * ux).sum(), (qs * uy).sum())) % 360.0)
        t_mean = float((qs * arr[m]).sum() / qs.sum())
        info[s] = (r_eff, mean_b, t_mean)
    tot = sum(S.values())
    norm = float(np.sqrt(tot.max())) or 1.0
    amp = row["peak_amp"]
    grid_t = t_a + np.arange(nb) / rate
    t_aud = t_a + np.arange(L) / sr
    out = np.zeros((2, L))
    for s, h in S.items():
        r_eff, mean_b, t_mean = info[s]
        env = np.interp(t_aud, grid_t, np.sqrt(h) / norm * amp)
        car = _shaped_noise(rng, L, sr, r_eff, temp_c, p_hpa, prm.rh_pct)
        mono = car * env
        head = float(camera.at(np.clip(t_mean, camera.t[0], camera.t[-1]))["heading_deg"])
        az = float(rel_azimuth_deg(mean_b, head))
        out += binaural(mono, sr, az)
        # a little diffuse spread (the rumble is reverberant): two short-delayed copies, one per ear
        d1, d2 = int(0.007 * sr), int(0.013 * sr)
        out[0, d1:] += 0.22 * mono[:-d1]
        out[1, d2:] += 0.22 * mono[:-d2]
    # the crack of a close strike, from the nearest source
    if row["dist_nearest_m"] < prm.crack_range_m:
        k = int(np.argmin(r))
        nc = int(0.35 * sr)
        tt = np.arange(nc) / sr
        env = np.minimum(tt / 0.002, 1.0) * np.exp(-tt / 0.06)
        c = _shaped_noise(rng, nc, sr, float(r[k]), temp_c, p_hpa, prm.rh_pct, base="crack") * env
        c *= 0.8 * amp * (1.0 - row["dist_nearest_m"] / prm.crack_range_m)
        c = binaural(c, sr, row["az_rel_nearest_deg"])
        i0 = int(round((float(arr[k]) - t_a) * sr))
        j1 = min(L, i0 + nc)
        if j1 > i0:
            out[:, i0:j1] += c[:, : j1 - i0]
    return int(round(t_a * sr)), out


def add_at(buf: np.ndarray, start: int, x: np.ndarray) -> None:
    """Add stereo ``x`` into ``buf`` at sample ``start`` (clipped to the buffer)."""
    n = buf.shape[1]
    a, b = max(0, start), min(n, start + x.shape[1])
    if b > a:
        buf[:, a:b] += x[:, a - start : b - start]

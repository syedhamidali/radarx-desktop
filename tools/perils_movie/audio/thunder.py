"""Thunder from every real LMA flash.

Physically grounded: every VHF source is a point emitter; its sound arrives at the listener
(the camera) at  t_source + r / 343 m/s  in data time, which is then mapped to clip time
(= flash clip time + r / 343 / speedup for a constant speedup). A long flash therefore starts at
its nearest part and rolls on as the farther parts arrive. The spectrum loses its top with range
through the ISO 9613-1 air absorption.

LEVEL IS COMPRESSED FOR AUDIBILITY; THE DELAY IS PHYSICAL. A strict 1/r law (20 dB per decade of range)
makes a flash 40 km away 20 dB quieter than one at 4 km and puts nearly all of a storm under the rain, so
the level follows ``L_ref - k log10(r / r_ref)`` with k = 12 dB per decade (``audibility_gain``); the air
absorption is applied for a compressed distance (``r_abs``) so a far flash is still a low rumble, not a
silence. Which flash is voiced is chosen by an audibility score (``select_flashes``), never invented:
every thunder is one real LMA flash with its row in the report.

Artistic: the noise carrier, the crack of a close strike, the energy proxy from the number of
sources and their relative power, the length of the tail. Synthesised, not a recording.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import lfilter

from .acoustics import (
    SPEED_OF_SOUND_MS,
    air_absorption_db_per_m,
    audibility_gain,
    binaural,
    db,
    distance_gain,
)
from .geo import bearing_deg, enu, rel_azimuth_deg
from .timeline import CameraPath, Timeline


@dataclass
class ThunderParams:
    level_model: str = "audibility"  # "audibility" (compressed, default) or "physical" (1/r)
    # amplitude (1.0 = full scale before the master) of a reference flash at r_ref_m
    a1: float = 0.1
    r_ref_m: float = 5000.0
    k_db_per_decade: float = 12.0  # 20 would be 1/r
    r_min_m: float = 1000.0  # no louder than this range (audibility model)
    r0_m: float = 300.0  # minimum range of the physical 1/r law
    floor_db: float = -42.0  # level floor of the physical law (reported when engaged)
    abs_ref_m: float = 3000.0  # air absorption uses r_abs = abs_ref * (r / abs_ref) ** 0.5
    max_range_m: float = 150_000.0  # beyond this a flash is not rendered
    env_rate: int = 1000
    n_sectors: int = 6
    rh_pct: float = 80.0  # assumed (not in the data)
    tail_s_per_km: float = 0.06
    tail_min_s: float = 0.5
    tail_max_s: float = 7.0
    crack_range_m: float = 12000.0
    # selection (see select_flashes): a clearly audible thunder in every ``guarantee_window_s`` of clip time
    # whenever a flash within ``guarantee_range_m`` is heard in it; then the best scores, at most
    # ``max_voices`` at once, ``window_cap`` starts per +-``window_s``, masking only below ``mask_db``
    guarantee_window_s: float = 8.0
    guarantee_range_m: float = 80_000.0
    max_voices: int = 3
    window_s: float = 1.0
    window_cap: int = 3
    mask_db: float = 20.0
    mask_s: float = 3.0
    tornado_bonus_db: float = 4.0  # score bonus for a flash within 10 km of the tornado
    n_bonus_db_per_decade: float = 3.0  # score bonus for the number of sources (relative to 50)


def flash_seed(seed: int, fid: str) -> int:
    return (seed * 1_000_003 + zlib.crc32(str(fid).encode())) & 0x7FFFFFFF


def plan_flash(
    flash, timeline: Timeline, camera: CameraPath, duration: float, prm: ThunderParams
) -> dict:
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
    if prm.level_model == "physical":
        A, floored = distance_gain(r, prm.a1 / 5.0, prm.r0_m, prm.floor_db)
    else:
        A, floored = audibility_gain(r, prm.a1, prm.r_ref_m, prm.k_db_per_decade, prm.r_min_m)
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
        listener={
            "lat": float(cam["lat"]),
            "lon": float(cam["lon"]),
            "alt_m": float(cam["alt_m"]),
            "heading_deg": float(cam["heading_deg"]),
        },
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
        score_db=float(db(a_flash))
        + prm.n_bonus_db_per_decade * float(np.log10(max(row["n_sources"], 1) / 50.0)),
        level_model=prm.level_model,
        level_floor_engaged=bool(floored.mean() > 0.5),
        power_dbw_median=float(pm),
        t_light_start_data_s=float(flash.t_start_s),
        _r=r,
        _arr=arr,
        _q=q,
        _e=e,
        _n=n,
        _u=u,
        _w=w,
        _cam=cam,
    )
    if r[k] > prm.max_range_m:
        row["status"] = "skipped_out_of_range"
    elif arr.min() >= duration or arr.max() + prm.tail_max_s < 0:
        row["status"] = "outside_clip"
    return row


def _r_abs(prm: ThunderParams, r_m: float) -> float:
    """Distance used for the air absorption. Audibility model: compressed (square root about abs_ref_m), so a
    far flash keeps its low and mid rumble; physical model: the real distance."""
    if prm.level_model == "physical":
        return float(r_m)
    return float(prm.abs_ref_m * (max(r_m, 1.0) / prm.abs_ref_m) ** 0.5)


def _tail_s(prm: ThunderParams, dist_m: float) -> float:
    return float(
        np.clip(
            prm.tail_min_s + prm.tail_s_per_km * dist_m / 1000.0, prm.tail_min_s, prm.tail_max_s
        )
    )


def select_flashes(
    rows: list[dict], prm: ThunderParams, duration: float | None = None
) -> dict[str, int]:
    """Choose which real flashes are voiced, by audibility score (``score_db``: level at the listener, number
    of sources, a bonus near the tornado) instead of blind thinning.

    1. Guarantee: in every ``guarantee_window_s`` of clip time (by first arrival) the best-scoring flash within
       ``guarantee_range_m`` is voiced (``selection = "guaranteed"``). A window with none that near but some
       farther (within ``max_range_m``) gets the best of those (``"guaranteed_distant"``, a long low rumble).
    2. The rest by score, with overlapping rolls allowed up to ``max_voices`` at once, at most ``window_cap``
       starts within +-``window_s``, and masked only when more than ``mask_db`` below a louder one at the
       same time.

    Sets row["status"] ("rendered" or "skipped_density" / "skipped_masked" / "skipped_voices") and
    row["selection"], and returns the count per status."""
    cand = [r for r in rows if r["status"] == "pending"]
    if duration is None:
        duration = max([r["last_arrival_clip_s"] for r in cand] + [1.0])
    dt = 0.1
    occ = np.zeros(int(np.ceil(duration / dt)) + 2, int)
    kept: list[dict] = []

    def span(r):
        a = r["first_arrival_clip_s"]
        i0 = max(0, int(np.floor(a / dt)))
        i1 = min(
            len(occ),
            int(
                np.ceil(
                    (
                        min(r["last_arrival_clip_s"], a + 4.0)  # the loud part of a long roll
                        + 0.5 * _tail_s(prm, r["dist_nearest_m"])
                    )
                    / dt
                )
            )
            + 1,
        )
        return i0, max(i1, i0 + 1)

    def accept(r, how):
        i0, i1 = span(r)
        occ[i0:i1] += 1
        r["status"] = "rendered"
        r["selection"] = how
        kept.append(r)

    # 1. the guarantee, window by window
    n_win = int(np.ceil(duration / prm.guarantee_window_s))
    for w in range(n_win):
        lo, hi = w * prm.guarantee_window_s, (w + 1) * prm.guarantee_window_s
        inwin = [r for r in cand if lo <= r["first_arrival_clip_s"] < hi]
        near = [r for r in inwin if r["dist_nearest_m"] <= prm.guarantee_range_m]
        pool, how = (near, "guaranteed") if near else (inwin, "guaranteed_distant")
        if pool:
            accept(max(pool, key=lambda r: r["score_db"]), how)
    # 2. the rest by score
    for r in sorted((r for r in cand if r["status"] == "pending"), key=lambda r: -r["score_db"]):
        a = r["first_arrival_clip_s"]
        if (
            len([k for k in kept if abs(k["first_arrival_clip_s"] - a) <= prm.window_s])
            >= prm.window_cap
        ):
            r["status"] = "skipped_density"
            continue
        if any(
            abs(k["first_arrival_clip_s"] - a) <= prm.mask_s
            and db(k["peak_amp"]) - db(r["peak_amp"]) > prm.mask_db
            for k in kept
        ):
            r["status"] = "skipped_masked"
            continue
        i0, i1 = span(r)
        if occ[i0:i1].max(initial=0) >= prm.max_voices:
            r["status"] = "skipped_voices"
            continue
        accept(r, "score")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


def _rumble_shape(f: np.ndarray) -> np.ndarray:
    """Source spectrum of the carrier: -6 dB/octave above 200 Hz, nothing below ~25 Hz."""
    return (1.0 / np.sqrt(1.0 + (f / 200.0) ** 2)) * (f**2 / (f**2 + 25.0**2))


def _shaped_noise(
    rng, n: int, sr: int, r_m: float, temp_c: float, p_hpa: float, rh: float, base: str = "rumble"
):
    """Unit-RMS noise with the source shape and the air absorption of ``r_m`` metres."""
    x = rng.standard_normal(n)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    alpha = air_absorption_db_per_m(np.maximum(f, 1.0), temp_c, rh, p_hpa)
    H = 10 ** (-alpha * r_m / 20.0)
    # the crack is flat above 500 Hz
    H = H * (_rumble_shape(f) if base == "rumble" else f**2 / (f**2 + 500.0**2))
    y = np.fft.irfft(X * H, n)
    s = np.sqrt(np.mean(y * y))
    return y / s if s > 0 else y


def render_flash(
    row: dict, sr: int, seed: int, camera: CameraPath, prm: ThunderParams, air: tuple[float, float]
):
    """Stereo audio of one planned flash: (start_sample, (2, L) array)."""
    rng = np.random.default_rng(flash_seed(seed, row["id"]))
    r, arr, q = row["_r"], row["_arr"], row["_q"]
    temp_c, p_hpa = air
    tau = _tail_s(prm, row["dist_nearest_m"])
    t_a = float(arr.min()) - 0.05
    span = float(arr.max() - arr.min()) + 0.1 + 4.5 * tau
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
        car = _shaped_noise(rng, L, sr, _r_abs(prm, r_eff), temp_c, p_hpa, prm.rh_pct)
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
        r_crack = min(
            _r_abs(prm, float(r[k])), 2000.0
        )  # a sharp crack even for a flash several km away
        c = _shaped_noise(rng, nc, sr, r_crack, temp_c, p_hpa, prm.rh_pct, base="crack") * env
        c *= 1.2 * amp * (1.0 - row["dist_nearest_m"] / prm.crack_range_m)
        c = binaural(c, sr, row["az_rel_nearest_deg"])
        i0 = round((float(arr[k]) - t_a) * sr)
        j1 = min(L, i0 + nc)
        if j1 > i0:
            out[:, i0:j1] += c[:, : j1 - i0]
    return round(t_a * sr), out


def add_at(buf: np.ndarray, start: int, x: np.ndarray) -> None:
    """Add stereo ``x`` into ``buf`` at sample ``start`` (clipped to the buffer)."""
    n = buf.shape[1]
    a, b = max(0, start), min(n, start + x.shape[1])
    if b > a:
        buf[:, a:b] += x[:, a - start : b - start]

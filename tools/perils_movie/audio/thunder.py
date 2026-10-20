"""Thunder from every real LMA flash, built from thunder acoustics (Few 1970; Holmes et al. 1971; Hill 1977;
Ribner and Roy 1982; Bass 1980).

Thunder is the sum of the sound radiated by every element of the lightning channel. Here every LMA VHF source
of a flash is one channel element at its real position (lat, lon, alt) and time:

* **Arrival**: the sound of source i reaches the listener (the camera's ground track) at
  ``t_i + r_i / 343 m/s``: the onset is the nearest (earliest) part of the channel, and a long or tilted channel
  rolls on as the farther parts arrive.
* **Time-lapse**: the clip is a time-lapse but thunder is not. The ONSET delay is ``r / 343 / speedup`` (the
  speedup at the flash); the arrivals after the onset keep their real spacing, so a thunder lasts as long as it
  would in real time (5 to 40 s) and its rolling keeps its character. In a real-time shot (speedup 1) all is
  exactly physical.
* **Pulse**: each source radiates an N-wave (``nwave.n_wave``): sharp and short close to the channel, a longer
  one with a slower front far away, with the ISO 9613-1 air absorption of its own distance applied as a
  minimum-phase filter (the highs of a far channel are gone, a close channel keeps its 1 to 5 kHz crack).
* **Low frequencies**: a damped 40 or 80 Hz pulse per source (heated-channel relaxation), the 80 Hz one where the
  local density of sources is high.
* **Tortuosity**: the LMA does not resolve the channel below ~100 m. Between consecutive sources closer than 3 km
  the channel is filled with extra elements at random spacing (1 to 20 m) and random lateral offsets (3 m),
  seeded: *sub-resolution tortuosity modelled* (statistical, not measured). It gives the crackle of a close channel.
* **Ground reflection** (listener at the ground): a delayed copy (``2 z_s h / r`` of path difference, coefficient
  0.5) of every source; **scattering and turbulence**: a weak slow amplitude modulation and a decaying
  sparse-echo tail whose length grows with the distance.
* **Binaural**: every arrival is panned by its azimuth relative to the camera heading at that time (equal-power
  level difference, Woodworth time difference, a cone-of-confusion reduction for elevated sources, a head-shadow
  low-pass on the far ear); a long flash is wide.

LEVEL IS COMPRESSED FOR AUDIBILITY; THE DELAY IS PHYSICAL (see ``acoustics.audibility_gain``). Not modelled:
refraction (wind and temperature gradients), terrain and building shadowing, the individual return strokes (the
LMA shows the channel, not the strokes), the true source strength (VHF power is not acoustic energy), the Doppler
shift of a moving camera. Synthesised from the data, not a recording.
"""

from __future__ import annotations

import heapq
import zlib
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import butter, fftconvolve, lfilter, sosfilt
from scipy.spatial import cKDTree

from .acoustics import (
    HEAD_RADIUS_M,
    SPEED_OF_SOUND_MS,
    audibility_gain,
    db,
    distance_gain,
    lateral_pan,
)
from .geo import bearing_deg, enu, rel_azimuth_deg
from .nwave import absorbed, accumulate, lf_pulse, n_wave, scatter_kernel
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
    # the ISO 9613-1 absorption is applied for this fraction of the real distance: 1 = physical; 0.5 keeps a far
    # thunder as bright as one half as far (the same compression as the level, so it stays audible on small speakers)
    absorption_scale: float = 0.5
    max_range_m: float = 150_000.0  # beyond this a flash is not rendered
    max_thunder_s: float = (
        15.0  # arrivals later than this after the onset are dropped (faded over the last 4 s)
    )
    fade_s: float = 4.0
    rh_pct: float = 80.0  # assumed (not in the data)
    tail_s_per_km: float = 0.06
    tail_min_s: float = 0.5
    tail_max_s: float = 7.0
    # synthesis
    n_dist_bins: int = 12
    crack_range_m: float = 15_000.0  # elements closer than this are boosted and get the sharp crack
    crack_boost: float = 3.0
    crack_gain: float = 1.5  # amplitude of the crack N-wave relative to the element's
    crack_absorb_m: float = (
        1500.0  # the crack keeps the brightness of a channel 1.5 km away at most
    )
    # low-frequency pulse amplitude relative to the N-wave: felt, not just heard, for the near and mid flashes
    lf_gain: float = 0.2  # far (> 30 km)
    lf_gain_near: float = 0.6  # < 8 km
    lf_gain_mid: float = 0.4  # 15 km (log interpolation between)
    lf_mid_hz: float = 110.0
    intra_flash_exponent: float = (
        1.0  # amplitude ~ r^-this between the parts of ONE flash (physical 1/r)
    )
    lf_low_hz: float = 55.0
    lf_high_hz: float = 80.0
    ground_reflection: float = 0.5
    ear_height_m: float = 1.7
    tortuosity: bool = True
    tort_r_max_m: float = (
        8000.0  # tortuosity only for channel parts closer than this (beyond it the highs are gone)
    )
    tort_spacing_m: tuple[float, float] = (1.0, 20.0)
    tort_sigma_m: float = 3.0
    tort_gain: float = 0.3
    tort_max_points: int = 20000
    bridge_gaps: bool = True  # fill arrival gaps longer than bridge_gap_s along the chord between the sources around them
    bridge_gap_s: float = 0.4
    bridge_gain: float = 0.25
    turbulence_depth_per_km: float = (
        0.012  # amplitude modulation depth: 0.04 + this x km, at most 0.3
    )
    scatter_gain_per_km: float = 0.01  # tail level: 0.2 + this x km (at most 0.5)
    # selection (see select_flashes): a clearly audible thunder in every ``guarantee_window_s`` of clip time
    # whenever a flash within ``guarantee_range_m`` is heard in it; then the best scores with variety, at most
    # ``max_voices`` at once, ``window_cap`` starts per +-``window_s``, masking only below ``mask_db``
    guarantee_window_s: float = 4.5
    guarantee_range_m: float = 150_000.0
    max_voices: int = 5
    max_coverage: float = 0.85  # thunder may sound for this fraction of any 10 s (loud parts)
    visible_bonus_db: float = (
        6.0  # score bonus for a flash that is in the camera's frame when it happens
    )
    view_aspect: float = 16.0 / 9.0
    view_margin_deg: float = 8.0
    window_s: float = 1.0
    window_cap: int = 4
    mask_db: float = 20.0
    mask_s: float = 3.0
    tornado_bonus_db: float = 4.0  # score bonus for a flash within 10 km of the tornado
    n_bonus_db_per_decade: float = (
        6.0  # score bonus for the number of sources (the big flashes), vs 50
    )
    variety_db: float = 3.0  # score penalty per voiced flash of the same range class within +-6 s
    variety_s: float = 6.0


_VARIANTS = (
    0.8,
    1.0,
    1.25,
)  # N-wave length variants (their spectral nulls fall at different frequencies)


def lf_gain_for(prm: ThunderParams, r_nearest_m: float) -> float:
    """Low-frequency gain by the distance of the flash: near, mid (15 km) and far, log-interpolated."""
    x = np.log([8000.0, 15000.0, 30000.0])
    y = [prm.lf_gain_near, prm.lf_gain_mid, prm.lf_gain]
    return float(np.interp(np.log(max(r_nearest_m, 1.0)), x, y))


def in_view(
    camera: CameraPath, t_clip: float, lat: float, lon: float, alt: float, prm: ThunderParams
):
    """Whether a point is in the camera's frame at a clip time (camera heading, pitch, vertical field of view
    and a 16:9 aspect, with a margin): (visible, angle from the view axis in degrees)."""
    c = camera.at(float(np.clip(t_clip, camera.t[0], camera.t[-1])))
    e, n, u = enu(c["lat"], c["lon"], c["alt_m"], lat, lon, alt)
    h, p = np.radians(c["heading_deg"]), np.radians(c["pitch_deg"])
    fwd = np.array([np.sin(h) * np.cos(p), np.cos(h) * np.cos(p), np.sin(p)])
    right = np.array([np.cos(h), -np.sin(h), 0.0])
    up = np.cross(right, fwd)
    d = np.array([float(e), float(n), float(u)])
    xf, xr, xu = float(d @ fwd), float(d @ right), float(d @ up)
    if xf <= 0.0:
        return False, 180.0
    vf = np.radians(float(c["fov_deg"]))
    hf = 2.0 * np.arctan(np.tan(vf / 2.0) * prm.view_aspect)
    m = np.radians(prm.view_margin_deg)
    ax, ay = np.arctan2(xr, xf), np.arctan2(xu, xf)
    ang = float(np.degrees(np.arccos(np.clip(xf / max(np.linalg.norm(d), 1e-9), -1.0, 1.0))))
    return bool(abs(ax) <= hf / 2 + m and abs(ay) <= vf / 2 + m), ang


def flash_seed(seed: int, fid: str) -> int:
    return (seed * 1_000_003 + zlib.crc32(str(fid).encode())) & 0x7FFFFFFF


def range_class(dist_m: float) -> int:
    """0 near (< 10 km, cracks and close rolls), 1 mid (10 to 30 km), 2 far (rumble)."""
    return 0 if dist_m < 10_000.0 else (1 if dist_m < 30_000.0 else 2)


def plan_flash(
    flash,
    timeline: Timeline,
    camera: CameraPath,
    duration: float,
    prm: ThunderParams,
    view_camera: CameraPath | None = None,
) -> dict:
    """Timing, distances and level of one flash for the camera as listener. No audio.

    The onset (first arrival) is the clock's clip time of the first sound (``flash + r / 343`` in data time);
    later arrivals follow in real time (``arrival_i - first arrival`` seconds)."""
    src = (
        flash.sources[np.argsort(flash.sources[:, 0], kind="stable")]
        if len(flash.sources)
        else flash.sources
    )
    row = {
        "id": flash.id,
        "n_sources": int(flash.n_sources or len(src)),
        "status": "pending",
    }
    if len(src) == 0:
        row["status"] = "no_sources"
        return row
    t_i = src[:, 0]
    t0 = float(t_i[0])
    tf = float(timeline.data_to_clip(t0))
    cam = camera.at(np.clip(tf, 0.0, duration))
    e, n, u = enu(cam["lat"], cam["lon"], cam["alt_m"], src[:, 1], src[:, 2], src[:, 3])
    r = np.sqrt(e * e + n * n + u * u)
    k = int(np.argmin(r))
    arr_real = t_i + r / SPEED_OF_SOUND_MS  # data time = real time of the sound
    a0 = float(arr_real.min())
    sp_f = float(timeline.speedup_at(np.clip(tf, 0.0, duration)))
    # the onset follows the clock through the time-lapse (= r / 343 / speedup for a constant speedup, and exact in a
    # real-time shot); the arrivals after it keep their real spacing
    onset = float(timeline.data_to_clip(a0))
    arr = onset + (arr_real - a0)
    p = src[:, 4]
    pm = np.nanmedian(p) if np.isfinite(p).any() else 0.0
    w = np.clip(10 ** ((np.nan_to_num(p, nan=pm) - pm) / 40.0), 0.4, 2.5)
    if prm.level_model == "physical":
        A, floored = distance_gain(r, prm.a1 / 5.0, prm.r0_m, prm.floor_db)
    else:
        A, floored = audibility_gain(r, prm.a1, prm.r_ref_m, prm.k_db_per_decade, prm.r_min_m)
    # local density of sources (within 1.5 km): a denser channel part radiates more
    if len(r) > 2:
        cnt = np.array(
            [
                len(x)
                for x in cKDTree(np.column_stack([e, n, u])).query_ball_point(
                    np.column_stack([e, n, u]), 1500.0
                )
            ]
        ).astype(float)
        rho = (cnt + 1.0) / (np.median(cnt) + 1.0)
    else:
        rho = np.ones(len(r))
    q = (w * A) ** 2
    nf = float(np.clip((row["n_sources"] / 50.0) ** 0.25, 0.6, 1.6))
    a_flash = float(np.sqrt(q.sum() / (w**2).sum()) * nf)
    cen = flash.centroid
    if np.isfinite(cen[0]):
        ce, cn, cu = enu(cam["lat"], cam["lon"], cam["alt_m"], cen[0], cen[1], cen[2])
        r_cen = float(np.sqrt(ce * ce + cn * cn + cu * cu))
    else:
        r_cen = float(np.mean(r))
    visible, view_angle = (False, None)
    if view_camera is not None and np.isfinite(flash.centroid[0]):
        visible, view_angle = in_view(
            view_camera, tf, flash.centroid[0], flash.centroid[1], flash.centroid[2], prm
        )
    row.update(
        visible_on_screen=bool(visible),
        view_angle_deg=view_angle,
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
        range_class=range_class(float(r[k])),
        bearing_nearest_deg=float(bearing_deg(e[k], n[k])),
        az_rel_nearest_deg=float(rel_azimuth_deg(bearing_deg(e[k], n[k]), cam["heading_deg"])),
        first_arrival_clip_s=float(onset),
        last_arrival_clip_s=float(min(arr.max(), onset + prm.max_thunder_s)),
        channel_extent_m=float(r.max() - r.min()),
        flash_duration_s=float(t_i[-1] - t0),
        delay_nearest_clip_s=float(
            timeline.data_to_clip(t_i[k] + r[k] / SPEED_OF_SOUND_MS) - timeline.data_to_clip(t_i[k])
        ),
        delay_nearest_real_s=float(r[k] / SPEED_OF_SOUND_MS),
        onset_delay_clip_s=float(onset - tf),
        speedup_at_flash=sp_f,
        peak_amp=a_flash,
        peak_dbfs_premaster=float(db(a_flash)),
        score_db=float(db(a_flash))
        + prm.n_bonus_db_per_decade * float(np.log10(max(row["n_sources"], 1) / 50.0))
        + (prm.visible_bonus_db if visible else 0.0),
        level_model=prm.level_model,
        level_floor_engaged=bool(floored.mean() > 0.5),
        power_dbw_median=float(pm),
        t_light_start_data_s=float(flash.t_start_s),
        _r=r,
        _arr=arr,
        _t=t_i,
        _a0=a0,
        _onset=float(onset),
        _A=A,
        _rho=rho,
        _e=e,
        _n=n,
        _u=u,
        _w=w,
        _cam=cam,
    )
    if r[k] > prm.max_range_m:
        row["status"] = "skipped_out_of_range"
    elif onset >= duration - 1.0 or arr.max() + prm.tail_max_s < 0:
        row["status"] = "outside_clip"
    return row


def _tail_s(prm: ThunderParams, dist_m: float) -> float:
    return float(
        np.clip(
            prm.tail_min_s + prm.tail_s_per_km * dist_m / 1000.0, prm.tail_min_s, prm.tail_max_s
        )
    )


def select_flashes(
    rows: list[dict], prm: ThunderParams, duration: float | None = None
) -> dict[str, int]:
    """Choose which real flashes are voiced, by audibility score (``score_db``: level at the listener, a bonus for
    the number of sources, a bonus near the tornado) with variety, instead of blind thinning.

    1. Guarantee: in every ``guarantee_window_s`` of clip time (by first arrival) the best-scoring flash within
       ``guarantee_range_m`` is voiced (``selection = "guaranteed"``). A window with none that near but some
       farther (within ``max_range_m``) gets the best of those (``"guaranteed_distant"``, a long low rumble).
    2. The rest by score, best first, with a penalty (``variety_db``) for every voiced flash of the same range
       class (near, mid, far) within ``variety_s`` so that cracks, rolls and rumble are mixed; overlapping rolls
       are allowed up to ``max_voices`` at once, at most ``window_cap`` starts within +-``window_s``, and a flash
       is masked only when more than ``mask_db`` below a louder one at the same time.

    Sets row["status"] ("rendered" or "skipped_density" / "skipped_masked" / "skipped_voices") and
    row["selection"], and returns the count per status."""
    cand = [r for r in rows if r["status"] == "pending"]
    if duration is None:
        duration = max([r["last_arrival_clip_s"] for r in cand] + [1.0])
    dt = 0.1
    occ = np.zeros(int(np.ceil(duration / dt)) + 2, int)
    cov_occ = np.zeros_like(
        occ
    )  # the loud part (the impact and its first roll) of every voiced thunder
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

    def loud_end(r, i0):
        # the loud part for the coverage: the impact and its first roll (2.5 s and a fifth of the tail)
        return min(len(occ), i0 + int((2.5 + 0.2 * _tail_s(prm, r["dist_nearest_m"])) / dt))

    def accept(r, how):
        i0, i1 = span(r)
        occ[i0:i1] += 1
        cov_occ[i0 : loud_end(r, i0)] += 1
        r["status"] = "rendered"
        r["selection"] = how
        kept.append(r)

    def adjusted(r):
        a = r["first_arrival_clip_s"]
        same = sum(
            1
            for k in kept
            if k["range_class"] == r["range_class"]
            and abs(k["first_arrival_clip_s"] - a) <= prm.variety_s
        )
        return r["score_db"] - prm.variety_db * same

    # 1. the guarantee, window by window
    n_win = int(np.ceil(duration / prm.guarantee_window_s))
    for w in range(n_win):
        lo, hi = w * prm.guarantee_window_s, (w + 1) * prm.guarantee_window_s
        inwin = [r for r in cand if lo <= r["first_arrival_clip_s"] < hi]
        near = [r for r in inwin if r["dist_nearest_m"] <= prm.guarantee_range_m]
        pool, how = (near, "guaranteed") if near else (inwin, "guaranteed_distant")
        if pool:
            accept(max(pool, key=lambda r: r["score_db"]), how)
    # 2. the rest by (variety-adjusted) score: a lazy priority queue, the penalty only ever lowers a score
    heap = [(-r["score_db"], i, r) for i, r in enumerate(cand) if r["status"] == "pending"]
    heapq.heapify(heap)
    while heap:
        neg, i, r = heapq.heappop(heap)
        adj = adjusted(r)
        if heap and adj < -heap[0][0] - 1e-9 and adj < -neg - 1e-9:
            heapq.heappush(heap, (-adj, i, r))
            continue
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
        # thunder may sound for at most max_coverage of any 10 s (the loud parts), so the rolls stay distinct
        w10 = int(10.0 / dt)
        lo, hi = max(0, i0 - w10), min(len(occ), i1 + w10)
        seg = cov_occ[lo:hi].copy()
        seg[i0 - lo : loud_end(r, i0) - lo] += 1
        if len(seg) >= w10:
            cov = np.convolve((seg > 0).astype(float), np.ones(w10) / w10, mode="valid")
            if cov.max() > prm.max_coverage:
                r["status"] = "skipped_coverage"
                continue
        accept(r, "score")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return counts


# --------------------------------------------------------------------------------------------- synthesis


def _tortuosity(rng, e, n, u, t, amp, r, prm: ThunderParams):
    """Sub-resolution tortuosity, modelled statistically: between consecutive sources (time order) closer than
    ``tort_r_max_m`` the chord is filled with elements at random spacing (1 to 20 m) with random lateral offsets
    (sigma ``tort_sigma_m``). Returns positions, emission times and amplitudes of the extra elements."""
    empty = (np.zeros(0),) * 5
    if len(e) < 2:
        return empty
    de, dn, du = np.diff(e), np.diff(n), np.diff(u)
    ln = np.sqrt(de * de + dn * dn + du * du)
    rm = np.minimum(r[:-1], r[1:])
    ok = np.nonzero((rm < prm.tort_r_max_m) & (ln > 3.0) & (ln < 3000.0))[0]
    if len(ok) == 0:
        return empty
    spacing = rng.uniform(prm.tort_spacing_m[0], prm.tort_spacing_m[1], len(ok))
    m = np.minimum(np.ceil(ln[ok] / spacing).astype(int), 400)
    order = np.argsort(rm[ok])  # nearest chords first when there are too many points
    cum = np.cumsum(m[order])
    keep = order[cum <= prm.tort_max_points]
    ok, m, spacing = ok[keep], m[keep], spacing[keep]
    if len(ok) == 0:
        return empty
    j = np.repeat(ok, m)
    f = rng.random(len(j))
    d = np.column_stack([de[j], dn[j], du[j]])
    dh = d / np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
    v1 = rng.standard_normal(d.shape)
    v1 -= (v1 * dh).sum(axis=1, keepdims=True) * dh
    v1 /= np.maximum(np.linalg.norm(v1, axis=1, keepdims=True), 1e-9)
    v2 = np.cross(dh, v1)
    off = (
        rng.standard_normal(len(j))[:, None] * v1 + rng.standard_normal(len(j))[:, None] * v2
    ) * prm.tort_sigma_m
    p0 = np.column_stack([e[j], n[j], u[j]])
    pos = p0 + f[:, None] * d + off
    tm = t[j] + f * (t[j + 1] - t[j])
    sp = np.repeat(spacing, m)
    am = 0.5 * (amp[j] + amp[j + 1]) * prm.tort_gain * np.sqrt(sp / 10.0)
    return pos[:, 0], pos[:, 1], pos[:, 2], tm, am


def _bridge(rng, e, n, u, t, r, amp, arr, onset, a0, prm: ThunderParams, c: float):
    """The VHF sources sample the channel, they do not cover it: where the arrivals of the real sources leave a
    gap longer than ``bridge_gap_s`` (real time), the straight chord between the two sources that arrive around
    it is filled with elements (20 ms of arrival per element, positions with a lateral scatter that grows with
    the chord length, amplitude ``bridge_gain`` of theirs). Modelled continuity, not measured: reported as
    ``bridged_s``. Returns positions, emission times, amplitudes and arrival times of the elements."""
    empty = (np.zeros(0),) * 5
    order = np.argsort(arr, kind="stable")
    a_s = arr[order]
    gaps = np.nonzero(np.diff(a_s) > prm.bridge_gap_s)[0]
    if len(gaps) == 0:
        return empty
    out = []
    for g in gaps:
        ia, ib = order[g], order[g + 1]
        gap = a_s[g + 1] - a_s[g]
        m = int(np.clip(np.ceil(gap / 0.02), 4, 400))
        f = (np.arange(m) + rng.random(m)) / m
        pa, pb = np.array([e[ia], n[ia], u[ia]]), np.array([e[ib], n[ib], u[ib]])
        d = pb - pa
        ln = float(np.linalg.norm(d))
        pos = (
            pa
            + f[:, None] * d
            + rng.standard_normal((m, 3)) * (prm.tort_sigma_m * (1.0 + ln / 1000.0))
        )
        tt = t[ia] + f * (t[ib] - t[ia])
        rr = np.linalg.norm(pos, axis=1)
        aa = onset + (tt + rr / c - a0)
        inside = (aa > a_s[g]) & (aa < a_s[g + 1])
        amp_el = np.full(m, 0.5 * (amp[ia] + amp[ib]) * prm.bridge_gain)
        out.append((pos[inside], tt[inside], amp_el[inside], aa[inside]))
    if not out:
        return empty
    pos = np.concatenate([o[0] for o in out])
    return (
        pos[:, 0],
        pos[:, 1],
        pos[:, 2],
        np.concatenate([o[1] for o in out]),
        np.concatenate([o[2] for o in out]),
    )


def _binaural_gains(e, n, u, heading_deg, sr):
    """Per arrival: (gain left, gain right, ITD samples left, ITD samples right, lateral position p)."""
    az = rel_azimuth_deg(bearing_deg(e, n), heading_deg)
    el = np.degrees(np.arctan2(u, np.hypot(e, n)))
    p = lateral_pan(az, el)
    ang = (0.7 * p + 1.0) * np.pi / 4.0
    th = np.arcsin(np.abs(p))
    d = np.round(HEAD_RADIUS_M / SPEED_OF_SOUND_MS * (th + np.sin(th)) * sr).astype(int)
    dl = np.where(p > 0, d, 0)
    dr = np.where(p < 0, d, 0)
    return np.cos(ang), np.sin(ang), dl, dr, p


def render_flash(
    row: dict,
    sr: int,
    seed: int,
    camera: CameraPath,
    prm: ThunderParams,
    air: tuple[float, float],
    ground_alt_m: float = 0.0,
):
    """Stereo audio of one planned flash: (start_sample, (2, L) array). Adds the synthesis statistics to ``row``
    (pulses, tortuosity points, reflections, duration, spectral centroid)."""
    rng = np.random.default_rng(flash_seed(seed, row["id"]))
    temp_c, p_hpa = air
    c = SPEED_OF_SOUND_MS
    onset, a0 = row["_onset"], row["_a0"]
    r, e, n, u, t = row["_r"], row["_e"], row["_n"], row["_u"], row["_t"]
    arr, w, rho = row["_arr"], row["_w"], row["_rho"]
    # inside one flash the parts keep their physical 1/r ratios (natural attack and decay); the compression is
    # applied to the level of the whole flash (the normalisation below)
    a_rel = (
        max(float(r.min()), prm.r_min_m) / np.maximum(r, prm.r_min_m)
    ) ** prm.intra_flash_exponent
    amp = a_rel * w * rho**0.25
    amp = amp * (
        1.0 + prm.crack_boost * np.clip((prm.crack_range_m - r) / prm.crack_range_m, 0.0, 1.0)
    )
    # tortuosity (sub-resolution, modelled): extra elements between consecutive sources
    te, tn, tu, tt, ta = (
        _tortuosity(rng, e, n, u, t, amp, r, prm) if prm.tortuosity else (np.zeros(0),) * 5
    )
    tr = np.sqrt(te * te + tn * tn + tu * tu)
    t_arr = onset + (tt + tr / c - a0)
    # the chord between the real sources that arrive around a gap (modelled continuity of the channel)
    be, bn, bu, bt, ba = (
        _bridge(rng, e, n, u, t, r, amp, arr, onset, a0, prm, c)
        if prm.bridge_gaps
        else (np.zeros(0),) * 5
    )
    br = np.sqrt(be * be + bn * bn + bu * bu)
    b_arr = onset + (bt + br / c - a0)
    # ground reflection of the primary sources (listener at the ground)
    z_s = row["_cam"]["alt_m"] + u - ground_alt_m
    h = np.hypot(e, n)
    hl = prm.ear_height_m
    d_path = (np.sqrt(h**2 + (z_s + hl) ** 2) - np.sqrt(h**2 + (z_s - hl) ** 2)) / c
    # all pulses: (kind 0 primary, 1 reflection, 2 tortuosity)
    nx = len(te) + len(be)
    P_e = np.concatenate([e, e, te, be])
    P_n = np.concatenate([n, n, tn, bn])
    P_u = np.concatenate([u, u, tu, bu])
    P_r = np.concatenate([r, r, tr, br])
    # a rough ground: the path difference of the reflection scatters by +-0.4 ms, so it does not comb the spectrum
    d_refl = np.maximum(d_path + rng.normal(0.0, 0.0004, len(d_path)), 0.0)
    P_arr = np.concatenate([arr, arr + d_refl, t_arr, b_arr])
    P_amp = np.concatenate([amp, amp * prm.ground_reflection, ta, ba])
    lfg = lf_gain_for(prm, float(r.min()))
    P_lf = np.concatenate([amp * lfg, amp * lfg * prm.ground_reflection, np.zeros(nx)])
    band = np.where(
        rho < 0.8, 0, np.where(rho < 1.3, 1, 2)
    )  # 40, 80 or 110 Hz by the local density
    P_hi = np.concatenate([band, band, np.zeros(nx, int)])
    # thunder lasts as long as the channel is long, up to max_thunder_s: the last fade_s seconds fade out
    x = np.clip((P_arr - onset - (prm.max_thunder_s - prm.fade_s)) / prm.fade_s, 0.0, 1.0)
    taper = np.cos(0.5 * np.pi * x) ** 2
    P_amp, P_lf = P_amp * taper, P_lf * taper
    live = taper > 1e-4
    n_prim_live = int(live[: len(r)].sum())
    P_e, P_n, P_u, P_r, P_arr, P_amp, P_lf, P_hi = (
        x[live] for x in (P_e, P_n, P_u, P_r, P_arr, P_amp, P_lf, P_hi)
    )
    heading = camera.at(np.clip(P_arr, camera.t[0], camera.t[-1]))["heading_deg"]
    gl, gr, dl, dr, lat_p = _binaural_gains(P_e, P_n, P_u, heading, sr)
    t_a = onset - 0.02
    span = float(P_arr.max() - onset) + 0.1
    scat_s = float(np.clip(0.5 + 0.05 * float(np.median(r)) / 1000.0, 0.5, 2.5))
    L = int((span + scat_s + 0.8) * sr) + 1
    idx = np.round((P_arr - t_a) * sr).astype(int)
    # distance bins: the kernel of each bin is the N-wave of its distance after the air absorption of it
    r_lo, r_hi = float(P_r.min()), float(P_r.max())
    ratio = max(1.3, (r_hi / max(r_lo, 1.0)) ** (1.0 / prm.n_dist_bins))
    b = np.floor(np.log(np.maximum(P_r, 1.0) / max(r_lo, 1.0)) / np.log(ratio)).astype(int)
    out = np.zeros((2, L))
    var = rng.integers(
        0, len(_VARIANTS), len(P_r)
    )  # which length variant of the N-wave each element has
    for bi in np.unique(b):
        m = b == bi
        rb = float(np.sqrt(P_r[m].min() * P_r[m].max()))
        r_abs = rb * prm.absorption_scale
        for v, scale in enumerate(_VARIANTS):
            mv = m & (var == v)
            if not mv.any():
                continue
            kern = absorbed(n_wave(sr, rb, scale), sr, r_abs, temp_c, p_hpa, prm.rh_pct)
            for ear, g, dd in ((0, gl, dl), (1, gr, dr)):
                accumulate(out[ear], idx[mv] + dd[mv], P_amp[mv] * g[mv], kern)
        for hi, f0 in ((0, prm.lf_low_hz), (1, prm.lf_high_hz), (2, prm.lf_mid_hz)):
            mm = m & (P_hi == hi) & (P_lf > 0)
            if not mm.any():
                continue
            kl = absorbed(lf_pulse(sr, f0), sr, r_abs, temp_c, p_hpa, prm.rh_pct)
            for ear, g, dd in ((0, gl, dl), (1, gr, dr)):
                accumulate(out[ear], idx[mm] + dd[mm], P_lf[mm] * g[mm], kl)
    # the crack of the channel parts within crack_range_m: a short N-wave that keeps the brightness of a channel
    # at most crack_absorb_m away (sharper than the rest), weaker towards the edge of the range
    for bi in np.unique(b):
        m = (b == bi) & (P_r < prm.crack_range_m)
        if not m.any():
            continue
        rb = float(np.sqrt(P_r[m].min() * P_r[m].max()))
        wc = prm.crack_gain * np.sqrt(np.clip(1.0 - P_r / prm.crack_range_m, 0.0, 1.0))
        for v, scale in enumerate(_VARIANTS):
            mv = m & (var == v)
            if not mv.any():
                continue
            kc = absorbed(
                n_wave(sr, min(rb, 1500.0), scale),
                sr,
                min(rb * prm.absorption_scale, prm.crack_absorb_m),
                temp_c,
                p_hpa,
                prm.rh_pct,
            )
            for ear, g, dd in ((0, gl, dl), (1, gr, dr)):
                accumulate(out[ear], idx[mv] + dd[mv], P_amp[mv] * wc[mv] * g[mv], kc)
    # head shadow on the ear that is farther from the energy (a low-pass blend)
    e_l, e_r = float((P_amp**2 * gl**2).sum()), float((P_amp**2 * gr**2).sum())
    far = 0 if e_l < e_r else 1
    shadow = float(
        np.clip((P_amp**2 * np.abs(lat_p)).sum() / max((P_amp**2).sum(), 1e-12), 0.0, 1.0) ** 1.5
    )
    a_lp = np.exp(-2 * np.pi * 1800.0 / sr)
    out[far] = (1 - shadow) * out[far] + shadow * lfilter([1 - a_lp], [1, -a_lp], out[far])
    # atmospheric turbulence: a weak slow amplitude modulation (depth grows with the distance)
    r_km = float(np.median(r)) / 1000.0
    depth = float(np.clip(0.04 + prm.turbulence_depth_per_km * r_km, 0.04, 0.3))
    for ear in (0, 1):
        nz = gaussian_filter1d(rng.standard_normal(L // 240 + 4), 2.0)
        nz /= max(nz.std(), 1e-9)
        out[ear] *= np.maximum(
            1.0 + depth * np.interp(np.arange(L) / sr, np.arange(len(nz)) * 240 / sr, nz), 0.0
        )
    # scattering by the ground and the turbulence: the long irregular tail
    w_s = float(np.clip(0.2 + prm.scatter_gain_per_km * r_km, 0.2, 0.5))
    for ear in (0, 1):
        sk = scatter_kernel(sr, rng, scat_s)
        out[ear] += w_s * fftconvolve(out[ear], sk)[:L]
    # nothing below 20 Hz: ears and loudspeakers do not reproduce infrasound
    out = sosfilt(butter(2, 20.0, btype="high", fs=sr, output="sos"), out, axis=1)
    # level: the loudest 100 ms (mean of the ears) is the audibility level of the flash
    win = sr // 10
    env = np.sqrt(np.convolve(out.mean(axis=0) ** 2, np.ones(win) / win, mode="same"))
    out *= row["peak_amp"] / max(float(env.max()), 1e-12)
    # statistics for the report
    m1 = out.mean(axis=0)
    env2 = np.sqrt(np.convolve(m1 * m1, np.ones(sr // 50) / (sr // 50), mode="same"))
    above = np.nonzero(env2 > 10 ** (-30 / 20) * env2.max())[0]
    spec = np.abs(np.fft.rfft(m1)) ** 2
    fr = np.fft.rfftfreq(len(m1), 1 / sr)
    row["n_pulses"] = n_prim_live
    row["n_reflections"] = int(live[len(r) : 2 * len(r)].sum())
    row["n_tortuosity_points"] = int(live[2 * len(r) : 2 * len(r) + len(te)].sum())
    row["n_bridge_elements"] = int(live[2 * len(r) + len(te) :].sum())
    row["tortuosity"] = (
        "sub-resolution tortuosity modelled (statistical, seeded)"
        if row["n_tortuosity_points"]
        else None
    )
    row["rendered_duration_30db_s"] = float((above[-1] - above[0]) / sr) if len(above) else 0.0
    row["spectral_centroid_hz"] = float((spec * fr).sum() / max(spec.sum(), 1e-30))
    return round(t_a * sr), out


def add_at(buf: np.ndarray, start: int, x: np.ndarray) -> None:
    """Add stereo ``x`` into ``buf`` at sample ``start`` (clipped to the buffer)."""
    n = buf.shape[1]
    a, b = max(0, start), min(n, start + x.shape[1])
    if b > a:
        buf[:, a:b] += x[:, a - start : b - start]

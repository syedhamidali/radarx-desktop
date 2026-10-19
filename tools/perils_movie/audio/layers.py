"""The continuous layers: rain, wind, ambient bed, convective rumble, tornado roar, hail.

Each ``render_*`` returns ``(stereo (2, N) array, info dict)``. Levels are in "pre-master" units
(1.0 = full scale before the loudness normalisation); the relative levels between layers are fixed
here, the master gain is set by the mix. The level CURVES are small pure functions (tested).

Physically grounded: the rain rate, Dm, wind speed / direction, the distances, the existence of a
tornado track, the hail events. Artistic: the timbre of every layer and the curve constants.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, lfilter, sosfilt

from .dsp import (
    banded_stereo,
    convolve_events,
    ctl_to_audio,
    damped_sine,
    impulse_train,
    local_rms_normalise,
    pan_gains,
    sum_kernels,
)
from .geo import rel_azimuth_deg
from .scene import CTL_RATE, Scene

# --------------------------------------------------------------------------- level curves

def rain_level_db(rate_mmh):
    """Perceptual (log) loudness of rain vs rain rate: -52 dB at 0 mm/h, +14 dB per decade of
    (1 + R): 1 mm/h -> -48, 10 -> -38.5, 100 -> -24 dB (pre-master). Monotonic."""
    return -52.0 + 14.0 * np.log10(1.0 + np.maximum(rate_mmh, 0.0))


def rain_gain(rate_mmh):
    r = np.maximum(np.asarray(rate_mmh, float), 0.0)
    return 10 ** (rain_level_db(r) / 20.0) * r / (r + 0.1)  # gate: no rain, no sound


def rain_distance_weight(d_m, d0_m=5000.0, floor=0.08):
    """Rain you are not under is heard only as a distant hiss."""
    d = np.asarray(d_m, float)
    return np.maximum(1.0 / (1.0 + (d / d0_m) ** 2), floor)


def wind_level_db(speed_ms):
    """Wind loudness vs speed: amplitude ~ v^1.5 (30 dB per decade), -38 dB at 10 m/s."""
    return -38.0 + 30.0 * np.log10(np.maximum(speed_ms, 0.3) / 10.0)


def wind_gain(speed_ms):
    v = np.maximum(np.asarray(speed_ms, float), 0.0)
    return 10 ** (wind_level_db(v) / 20.0) * v / (v + 0.5)


def wind_cutoff_hz(speed_ms):
    """Spectral tilt: a stronger wind is brighter (same law as the earlier storm clip)."""
    return 160.0 + 38.0 * np.clip(speed_ms, 0.0, 60.0)


def tornado_gain(d_m, a_1km=0.25, r0_m=300.0, reach_m=25000.0):
    """Roar amplitude vs distance: 1/r (minimum range r0), fading to zero at ``reach_m``."""
    d = np.asarray(d_m, float)
    a = a_1km * 1000.0 / np.maximum(d, r0_m)
    return np.where(np.isfinite(d), a * np.clip(1.0 - d / reach_m, 0.0, 1.0) ** 2, 0.0)


def tornado_cutoff_hz(d_m):
    """Muffled when far: 3.5 kHz close, 120 Hz floor at long range."""
    d = np.where(np.isfinite(d_m), d_m, 1e6)
    return np.maximum(120.0, 3500.0 / (1.0 + d / 1500.0))


# --------------------------------------------------------------------------- rain

DROP_D_MM = np.array([0.6, 1.2, 2.0, 3.2, 5.0])


def render_rain(scene: Scene, sr: int, n: int, seed: int):
    rng = np.random.default_rng(seed)
    R, d, ok = scene.blend("rain_rate_mmh")
    R = np.where(ok, np.maximum(R, 0.0), 0.0)
    dm, _, okd = scene.blend("dm_mm")
    dm_assumed = ~okd
    dm = np.where(okd & (dm > 0), dm, np.clip(0.9 * np.maximum(R, 0.01) ** 0.21, 0.5, 3.0))
    wd = np.where(ok, rain_distance_weight(np.where(np.isfinite(d), d, 1e9)), 0.0)
    level = rain_gain(R) * wd
    # patter: Poisson drop impacts; class mix from an exponential drop-size distribution with median
    # volume diameter Dm (Lambda = 3.67 / Dm), energy weighted (D^3)
    lam_ctl = 60.0 * np.maximum(R, 0.0) ** 0.6 * wd**2
    lam = ctl_to_audio(lam_ctl, n, sr) / sr
    idx, amps = impulse_train(rng, n, lam)
    L = 3.67 / np.maximum(dm, 0.3)
    w_c = np.exp(-np.outer(DROP_D_MM, L)) * (DROP_D_MM**3)[:, None]  # (C, nctl)
    w_c /= w_c.sum(axis=0, keepdims=True)
    cum = np.cumsum(w_c, axis=0)  # (C, nctl)
    ci = np.clip((idx / sr * CTL_RATE).astype(int), 0, cum.shape[1] - 1)
    u = rng.random(len(idx))
    cls = (u[None, :] > cum[:, ci]).sum(axis=0).clip(0, len(DROP_D_MM) - 1)
    patter = np.zeros((2, n))
    for c, D in enumerate(DROP_D_MM):
        k = damped_sine(sr, 5500.0 / D**0.7, 0.0015 + 0.0008 * D) * D**1.5
        for ch in (0, 1):
            sel = (cls == c) & (rng.random(len(idx)) < 0.5)  # each impact is heard mostly at one ear
            patter[ch] += convolve_events(n, idx[sel], amps[sel] * 1.4, k)
    # hiss: fine drops and the sum of many small impacts
    hb, ha = butter(1, 2500.0, btype="high", fs=sr)
    hiss = lfilter(hb, ha, rng.standard_normal((2, n)), axis=1)
    pw = np.clip((dm - 0.4) / 1.6, 0.1, 0.9) * wd**2
    pw_a = ctl_to_audio(pw, n, sr)
    out = np.zeros((2, n))
    for ch in (0, 1):
        pn = local_rms_normalise(patter[ch], sr)
        hn = local_rms_normalise(hiss[ch], sr, floor=1e-2)
        out[ch] = (np.sqrt(pw_a) * pn + np.sqrt(1.0 - pw_a) * hn) * ctl_to_audio(level, n, sr)
    info = {
        "rain_rate_mmh": {"min": float(R.min()), "mean": float(R.mean()), "max": float(R.max())},
        "dm_mm": {"mean": float(dm.mean()), "max": float(dm.max()), "assumed_fraction": float(dm_assumed.mean())},
        "coverage": float(ok.mean()),
        "mean_station_distance_m": float(np.mean(d[ok])) if ok.any() else None,
        "level_db_premaster": {"min": float(20 * np.log10(max(level.min(), 1e-9))),
                               "max": float(20 * np.log10(max(level.max(), 1e-9)))},
    }
    return out, info


# --------------------------------------------------------------------------- wind

def _station_ti(scene: Scene):
    """Turbulence intensity (std / mean of the observed speed over +-2 samples), or from the gust
    column when a station has one; blended like the speed."""
    w, _ = scene.station_weights("wind_speed_ms")
    rows = []
    for s in scene.bundle.stations:
        if "wind_speed_ms" not in s.series:
            rows.append(np.full(scene.n, np.nan))
            continue
        t, v = s.series["wind_speed_ms"]
        m = np.isfinite(v)
        t, v = t[m], v[m]
        if len(t) < 3:
            rows.append(np.full(scene.n, 0.15))
            continue
        gust = s.series.get("wind_gust_ms")
        if gust is not None:
            gt, gv = gust
            gm = np.isfinite(gv)
            gf = np.interp(t, gt[gm], gv[gm]) / np.maximum(v, 0.5) if gm.any() else None
        else:
            gf = None
        if gf is not None:
            ti = np.clip((gf - 1.0) / 2.5, 0.05, 0.5)
        else:
            k = 2
            ti = np.array([
                np.std(v[max(0, i - k): i + k + 1]) / max(np.mean(v[max(0, i - k): i + k + 1]), 1.0)
                for i in range(len(v))
            ])
            ti = np.clip(ti, 0.05, 0.5)
        rows.append(np.interp(scene.tw, t, ti))
    ti = np.array(rows) if rows else np.zeros((0, scene.n))
    sw = np.maximum(w.sum(axis=0), 1e-12)
    return np.clip((w * np.nan_to_num(ti, nan=0.15)).sum(axis=0) / sw, 0.08, 0.45)


def render_wind(scene: Scene, sr: int, n: int, seed: int, gust_hz: float = 0.8):
    rng = np.random.default_rng(seed)
    v, d, ok = scene.blend("wind_speed_ms")
    v = np.where(ok, np.maximum(v, 0.0), 0.0)
    ti = _station_ti(scene)
    # gusts: slow noise, unit variance (the amplitude from the observed variability, the fine
    # structure is synthetic)
    sos = butter(2, gust_hz, fs=CTL_RATE, output="sos")
    g = sosfilt(sos, rng.standard_normal(scene.n + 400))[400:]
    g = g / max(g.std(), 1e-9)
    veff = np.maximum(v * (1.0 + ti * g), 0.0)
    wd = np.where(ok, np.maximum(1.0 / (1.0 + (np.where(np.isfinite(d), d, 1e9) / 20000.0) ** 2), 0.5), 0.0)
    level = wind_gain(veff) * wd
    dirn, okd = scene.blend_wind_vector()
    rel = rel_azimuth_deg(dirn, scene.heading)  # wind FROM this side
    out = banded_stereo(n, sr, level, wind_cutoff_hz(veff), seed + 1, rho=0.5, pan_ctl=np.where(okd, rel, 0.0))
    info = {
        "wind_speed_ms": {"min": float(v.min()), "mean": float(v.mean()), "max": float(v.max())},
        "turbulence_intensity_mean": float(ti.mean()),
        "coverage": float(ok.mean()),
        "level_db_premaster": {"min": float(20 * np.log10(max(level.min(), 1e-9))),
                               "max": float(20 * np.log10(max(level.max(), 1e-9)))},
        "gust_front_max_rise_ms_per_clip_s": float(np.max(np.diff(v)) * CTL_RATE) if len(v) > 1 else 0.0,
    }
    return out, info


# --------------------------------------------------------------------------- ambient + convective

def render_ambient(sr: int, n: int, seed: int, level_db: float = -64.0):
    """A very quiet pre-storm bed: low-passed pink-ish NOISE (clearly synthetic: no insects, no room
    tone, no music)."""
    rng = np.random.default_rng(seed)
    out = np.zeros((2, n))
    f = np.fft.rfftfreq(n, 1 / sr)
    shape = 1.0 / np.sqrt(np.maximum(f, 20.0)) / np.sqrt(1.0 + (f / 1200.0) ** 4)
    for ch in (0, 1):
        x = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * shape, n)
        out[ch] = x / x.std()
    sw = 1.0 + 0.25 * np.sin(2 * np.pi * 0.07 * np.arange(n) / sr + rng.random() * 6.28)
    out *= 10 ** (level_db / 20.0) * sw
    return out, {"level_db_premaster": level_db, "note": "synthetic noise bed"}


def render_convective(scene: Scene, sr: int, n: int, seed: int, max_db: float = -40.0):
    """A gentle low rumble that follows the convective-core index at the listener (none in
    stratiform / where the index is zero)."""
    idx, source = scene.convective()
    rng = np.random.default_rng(seed)
    f = np.fft.rfftfreq(n, 1 / sr)
    shape = (1.0 / np.maximum(f, 25.0)) * (f**2 / (f**2 + 25.0**2)) / np.sqrt(1.0 + (f / 160.0) ** 4)
    out = np.zeros((2, n))
    for ch in (0, 1):
        x = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * shape, n)
        out[ch] = x / x.std()
    slow = 1.0 + 0.3 * ctl_to_audio(
        sosfilt(butter(2, 0.3, fs=CTL_RATE, output="sos"), rng.standard_normal(scene.n)) * 3.0, n, sr
    )
    out *= ctl_to_audio(idx, n, sr) * 10 ** (max_db / 20.0) * np.clip(slow, 0.4, 1.6)
    return out, {"source": source, "convective_fraction_of_clip": float((idx > 0.05).mean()),
                 "max_db_premaster": max_db}


# --------------------------------------------------------------------------- tornado

def render_tornado(scene: Scene, sr: int, n: int, seed: int):
    """STYLISED 'freight train' roar + debris rattle. Level = f(distance listener -> observed
    tornado position) while the track / events say it exists; exactly zero without evidence."""
    dist, exist, srcs = scene.tornado()
    if not exist.any():
        return np.zeros((2, n)), {"evidence": [], "active_seconds": 0.0, "stylised": True,
                                  "note": "no tornado evidence in the window: silent"}
    rng = np.random.default_rng(seed)
    a = tornado_gain(dist) * exist
    bearing = scene.tornado_bearing()
    rel = rel_azimuth_deg(bearing, scene.heading)
    roar = banded_stereo(n, sr, a, tornado_cutoff_hz(dist), seed + 3, rho=0.7,
                         centers=(40.0, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0))
    # a rotating throb (the structure of the roar), 1.5 to 2.5 Hz, ~25 %
    t = np.arange(n) / sr
    throb = 1.0 + 0.25 * np.sin(2 * np.pi * 2.0 * t + 0.4 * np.sin(2 * np.pi * 0.11 * t))
    roar *= throb
    # directional placement: half focused to the bearing, half diffuse
    gl, gr = pan_gains(ctl_to_audio(rel, n, sr))
    mono = roar.mean(axis=0)
    out = 0.5 * roar + 0.5 * np.stack([mono * gl * np.sqrt(2), mono * gr * np.sqrt(2)])
    # debris rattle: Poisson clicks, only when within 6 km
    prox = np.clip(1.0 - dist / 6000.0, 0.0, 1.0) ** 2 * exist
    lam = ctl_to_audio(40.0 * prox, n, sr) / sr
    idx, amps = impulse_train(rng, n, lam, 0.8)
    kern = sum_kernels(damped_sine(sr, 1800.0, 0.004), 0.5 * damped_sine(sr, 3400.0, 0.002))
    for ch in (0, 1):
        sel = rng.random(len(idx)) < 0.5
        r = convolve_events(n, idx[sel], amps[sel], kern)
        out[ch] += r * ctl_to_audio(tornado_gain(dist) * prox, n, sr) * 0.6
    info = {
        "evidence": srcs, "stylised": True,
        "active_seconds": float((exist > 0.5).mean() * scene.duration),
        "nearest_distance_m": float(np.min(dist[np.isfinite(dist)])) if np.isfinite(dist).any() else None,
        "max_level_db_premaster": float(20 * np.log10(max(a.max(), 1e-9))),
        "note": "STYLISED sound; position from the survey / radar evidence, timbre invented",
    }
    return out, info


# --------------------------------------------------------------------------- hail

def render_hail(scene: Scene, sr: int, n: int, seed: int):
    """Hail impacts (ground / car roofs) only where hail events / radar hail class say so near the
    listener. Density and loudness scale with Dm and rain rate. Without evidence: silence."""
    h = scene.hail()
    if not (h["w"] > 0).any():
        return np.zeros((2, n)), {"evidence": [], "active_seconds": 0.0, "events": 0,
                                  "note": "no hail evidence: silent"}
    rng = np.random.default_rng(seed)
    w, dm, rr = h["w"], np.maximum(h["dm_mm"], 2.0), np.maximum(h["rr_mmh"], 5.0)
    lam_ctl = 35.0 * w * np.clip(dm / 10.0, 0.3, 2.0) * (rr / 50.0) ** 0.4
    idx, amps = impulse_train(rng, n, ctl_to_audio(lam_ctl, n, sr) / sr, 0.6)
    amp_ctl = 0.22 * (dm / 10.0) ** 1.5 * w
    amps = amps * ctl_to_audio(amp_ctl, n, sr)[idx]
    big = ctl_to_audio(np.clip((dm - 5.0) / 20.0, 0.0, 1.0), n, sr)[idx] > rng.random(len(idx))
    pans = rng.uniform(-0.9, 0.9, len(idx))
    gl, gr = pan_gains(np.degrees(np.arcsin(pans)))
    out = np.zeros((2, n))
    for sel, k in (
        (~big, sum_kernels(damped_sine(sr, 3200.0, 0.004), 0.4 * damped_sine(sr, 1500.0, 0.008))),
        (big, sum_kernels(damped_sine(sr, 1500.0, 0.009), 0.5 * damped_sine(sr, 700.0, 0.015))),
    ):
        out[0] += convolve_events(n, idx[sel], (amps * gl)[sel], k)
        out[1] += convolve_events(n, idx[sel], (amps * gr)[sel], k)
    info = {
        "evidence": h["sources"], "events": int(len(idx)), "evidence_events": h["n_events"],
        "active_seconds": float((w > 0.02).mean() * scene.duration),
        "dm_mm_mean_active": float(dm[w > 0.02].mean()),
        "note": "impact transients; only where hail events / radar hail class were reported",
    }
    return out, info

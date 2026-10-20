"""Level curves and the ambient bed, convective rumble and tornado roar (rain, wind and hail are in weather.py).

Each ``render_*`` returns ``(stereo (2, N) array, info dict)``. Levels are in "pre-master" units
(1.0 = full scale before the loudness normalisation); the relative levels between layers are fixed
here, the master gain is set by the mix. The level CURVES are small pure functions (tested).

Physically grounded: the rain rate, Dm, wind speed / direction, the distances, the existence of a
tornado track, the hail events. Artistic: the timbre of every layer and the curve constants.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

from .dsp import (
    banded_stereo,
    convolve_events,
    ctl_to_audio,
    damped_sine,
    impulse_train,
    pan_gains,
    sum_kernels,
)
from .geo import rel_azimuth_deg
from .scene import CTL_RATE, Scene

# --------------------------------------------------------------------------- level curves


def rain_level_db(rate_mmh):
    """Perceptual (log) loudness of rain vs rain rate: -58 dB at 0 mm/h, +14 dB per decade of
    (1 + R): 1 mm/h -> -54, 10 -> -44.5, 100 -> -30 dB (pre-master). Monotonic."""
    return -58.0 + 14.0 * np.log10(1.0 + np.maximum(rate_mmh, 0.0))


def rain_gain(rate_mmh):
    r = np.maximum(np.asarray(rate_mmh, float), 0.0)
    return 10 ** (rain_level_db(r) / 20.0) * r / (r + 0.1)  # gate: no rain, no sound


def rain_dm_offset_db(dm_mm):
    """Bigger drops hit harder: +3 dB per doubling of Dm around 1.5 mm (limited to -4.5 / +6 dB)."""
    return 3.0 * np.clip(np.log2(np.maximum(np.asarray(dm_mm, float), 0.3) / 1.5), -1.5, 2.0)


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


def tornado_gain(d_m, a_3km=0.018, r_min_m=1500.0, k_db_per_decade=12.0, reach_m=25000.0):
    """Roar amplitude vs distance, compressed like the thunder (12 dB per decade instead of 20, no louder than
    at 1.5 km: a roar next to the tornado would bury every other layer once the mix is normalised), fading to
    zero at ``reach_m``. Monotonic in the distance."""
    d = np.asarray(d_m, float)
    a = a_3km * (3000.0 / np.maximum(d, r_min_m)) ** (k_db_per_decade / 20.0)
    return np.where(np.isfinite(d), a * np.clip(1.0 - d / reach_m, 0.0, 1.0) ** 2, 0.0)


def tornado_cutoff_hz(d_m):
    """Muffled when far: 3.5 kHz close, 120 Hz floor at long range."""
    d = np.where(np.isfinite(d_m), d_m, 1e6)
    return np.maximum(120.0, 3500.0 / (1.0 + d / 1500.0))


# --------------------------------------------------------------------------- ambient + convective


def render_ambient(sr: int, n: int, seed: int, level_db: float = -54.0):
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


def render_convective(scene: Scene, sr: int, n: int, seed: int, max_db: float = -44.0):
    """A gentle low rumble that follows the convective-core index at the listener (none in
    stratiform / where the index is zero)."""
    idx, source = scene.convective()
    rng = np.random.default_rng(seed)
    f = np.fft.rfftfreq(n, 1 / sr)
    shape = (
        (1.0 / np.maximum(f, 25.0)) * (f**2 / (f**2 + 25.0**2)) / np.sqrt(1.0 + (f / 160.0) ** 4)
    )
    out = np.zeros((2, n))
    for ch in (0, 1):
        x = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * shape, n)
        out[ch] = x / x.std()
    slow = 1.0 + 0.3 * ctl_to_audio(
        sosfilt(butter(2, 0.3, fs=CTL_RATE, output="sos"), rng.standard_normal(scene.n)) * 3.0,
        n,
        sr,
    )
    out *= ctl_to_audio(idx, n, sr) * 10 ** (max_db / 20.0) * np.clip(slow, 0.4, 1.6)
    return out, {
        "source": source,
        "convective_fraction_of_clip": float((idx > 0.05).mean()),
        "max_db_premaster": max_db,
    }


# --------------------------------------------------------------------------- tornado


def render_tornado(scene: Scene, sr: int, n: int, seed: int):
    """STYLISED 'freight train' roar + debris rattle. Level = f(distance listener -> observed
    tornado position) while the track / events say it exists; exactly zero without evidence."""
    dist, exist, srcs = scene.tornado()
    if not exist.any():
        return np.zeros((2, n)), {
            "evidence": [],
            "active_seconds": 0.0,
            "stylised": True,
            "note": "no tornado evidence in the window: silent",
        }
    rng = np.random.default_rng(seed)
    a = tornado_gain(dist) * exist
    bearing = scene.tornado_bearing()
    rel = rel_azimuth_deg(bearing, scene.heading)
    roar = banded_stereo(
        n,
        sr,
        a,
        tornado_cutoff_hz(dist),
        seed + 3,
        rho=0.7,
        centers=(40.0, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0),
    )
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
        "evidence": srcs,
        "stylised": True,
        "active_seconds": float((exist > 0.5).mean() * scene.duration),
        "nearest_distance_m": float(np.min(dist[np.isfinite(dist)]))
        if np.isfinite(dist).any()
        else None,
        "max_level_db_premaster": float(20 * np.log10(max(a.max(), 1e-9))),
        "note": "STYLISED sound; position from the survey / radar evidence, timbre invented",
    }
    return out, info

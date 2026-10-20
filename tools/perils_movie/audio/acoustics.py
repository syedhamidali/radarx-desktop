"""Physics and psychoacoustics shared by the layers.

Physically grounded: speed of sound (343 m/s), the light-to-sound delay, 1/r spreading, the
frequency-dependent atmospheric absorption of ISO 9613-1, the interaural time difference
(Woodworth) and a simple head-shadow. Artistic: every timbre (see docs/PERILS_AUDIO.md).
"""

from __future__ import annotations

import numpy as np
from scipy.signal import lfilter

SPEED_OF_SOUND_MS = 343.0
HEAD_RADIUS_M = 0.0875


def thunder_delay_s(distance_m, speedup: float = 1.0):
    """Clip seconds between the flash and the sound reaching the listener:
    distance / 343 m/s (real physics, in data seconds) divided by the time-lapse speedup."""
    return np.asarray(distance_m, float) / SPEED_OF_SOUND_MS / float(speedup)


def air_absorption_db_per_m(
    f_hz, temp_c: float = 15.0, rh_pct: float = 80.0, p_hpa: float = 1000.0
):
    """Pure-tone atmospheric absorption coefficient, ISO 9613-1:1993 (dB per metre)."""
    f = np.asarray(f_hz, float)
    T = temp_c + 273.15
    T0, T01 = 293.15, 273.16
    pa = p_hpa / 1013.25
    C = -6.8346 * (T01 / T) ** 1.261 + 4.6151
    h = rh_pct * 10**C / pa
    frO = pa * (24.0 + 4.04e4 * h * (0.02 + h) / (0.391 + h))
    frN = pa * (T / T0) ** -0.5 * (9.0 + 280.0 * h * np.exp(-4.170 * ((T / T0) ** (-1 / 3) - 1.0)))
    f2 = f * f
    a = (
        8.686
        * f2
        * (
            1.84e-11 / pa * (T / T0) ** 0.5
            + (T / T0) ** -2.5
            * (
                0.01275 * np.exp(-2239.1 / T) / (frO + f2 / frO)
                + 0.1068 * np.exp(-3352.0 / T) / (frN + f2 / frN)
            )
        )
    )
    return a


def distance_gain(r_m, a1: float = 0.35, r0_m: float = 300.0, floor_db: float = -42.0):
    """Linear amplitude of a sound source heard at range r: 1/r spreading with a minimum range
    ``r0_m`` (no singularity for a flash overhead) and a level floor. ``a1`` is the amplitude at
    1 km. Returns (amplitude, floor_engaged)."""
    r = np.maximum(np.asarray(r_m, float), r0_m)
    a = a1 * 1000.0 / r
    lo = a1 * 10 ** (floor_db / 20)
    return np.maximum(a, lo), a < lo


def audibility_gain(
    r_m,
    a_ref: float = 0.35,
    r_ref_m: float = 5000.0,
    k_db_per_decade: float = 12.0,
    r_min_m: float = 1000.0,
):
    """Amplitude of a flash heard at range r, COMPRESSED FOR AUDIBILITY (the delay stays physical):
    ``L_ref - k log10(r / r_ref)`` with k = 12 dB per decade instead of the 20 of 1/r, no louder than at
    ``r_min_m``. Returns (amplitude, False); same signature shape as ``distance_gain``."""
    r = np.maximum(np.asarray(r_m, float), r_min_m)
    a = a_ref * 10 ** (-k_db_per_decade * np.log10(r / r_ref_m) / 20.0)
    return a, np.zeros(np.shape(a), bool)


def db(x, floor=1e-12):
    return 20.0 * np.log10(np.maximum(np.abs(x), floor))


def lateral_pan(az_rel_deg, el_deg: float = 0.0):
    """Lateral position p in [-1, 1] (-1 = left, +1 = right): sin of the lateral angle.
    Sources behind map to the same side as in front (cone of confusion)."""
    az = np.radians(az_rel_deg)
    return np.clip(np.sin(az) * np.cos(np.radians(el_deg)), -1.0, 1.0)


def binaural(mono: np.ndarray, sr: int, az_rel_deg: float, el_deg: float = 0.0) -> np.ndarray:
    """Place a mono signal at an azimuth (deg, + right): (2, N) float array.

    No HRTF data are used. Equal-power level difference, Woodworth interaural time difference
    (<= 0.66 ms) and a head-shadow low-pass on the far ear (cutoff falls from 20 kHz in front to
    ~1.8 kHz at 90 deg). Sources behind lose some top end (pinna).
    """
    p = float(lateral_pan(az_rel_deg, el_deg))
    th = np.arcsin(abs(p))
    itd = HEAD_RADIUS_M / SPEED_OF_SOUND_MS * (th + np.sin(th))
    d = round(itd * sr)
    ang = (
        (0.7 * p + 1.0) * np.pi / 4.0
    )  # +-0.7: about 13 dB between the ears at 90 deg, not a hard pan
    gl, gr = np.cos(ang), np.sin(ang)
    x = mono.astype(np.float64)
    behind = abs(float(az_rel_deg)) > 90.0
    if behind:
        x = _blend_lowpass(x, sr, 5500.0, 0.35)
    near = x
    far = _blend_lowpass(x, sr, 1800.0, abs(p) ** 1.5) if abs(p) > 0.02 else x
    if d > 0:
        far = np.concatenate([np.zeros(d), far[:-d]]) if d < len(far) else np.zeros_like(far)
    # the near ear is the louder; equal power overall
    if p >= 0:  # source on the right: right = near
        left, right = far * gl, near * gr
    else:
        left, right = near * gl, far * gr
    return np.stack([left, right])


def _blend_lowpass(x, sr, fc, mix):
    a = np.exp(-2 * np.pi * fc / sr)
    lp = lfilter([1 - a], [1, -a], x)
    return (1 - mix) * x + mix * lp


def rms(x):
    x = np.asarray(x, float)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def spectral_centroid(x, sr):
    x = np.asarray(x, float)
    s = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / sr)
    return float((s * f).sum() / max(s.sum(), 1e-30))

"""Small DSP building blocks shared by the layers (numpy / scipy only, all seeded)."""

from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import butter, fftconvolve, sosfilt

from .scene import CTL_RATE

OCTAVE_CENTERS = (63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0)


def ctl_to_audio(x: np.ndarray, n: int, sr: int) -> np.ndarray:
    """A control-rate (100 Hz) series to audio rate, linear interpolation."""
    t = np.arange(n) / sr
    return np.interp(t, np.arange(len(x)) / CTL_RATE, x)


def pan_gains(az_rel_deg):
    """Equal-power left/right gains for azimuth(s) relative to the heading (+ right); the range
    between the ears is limited to ~13 dB (see acoustics.binaural). Arrays broadcast."""
    p = np.sin(np.radians(az_rel_deg))
    ang = (0.7 * p + 1.0) * np.pi / 4.0
    return np.cos(ang), np.sin(ang)


def band(x: np.ndarray, sr: int, fc: float) -> np.ndarray:
    """One-octave band of x around fc (2nd-order Butterworth band-pass), unit RMS."""
    lo, hi = fc / np.sqrt(2), min(fc * np.sqrt(2), 0.45 * sr)
    sos = butter(2, [lo, hi], btype="band", fs=sr, output="sos")
    y = sosfilt(sos, x)
    s = y.std()
    return y / s if s > 0 else y


def lowpass_mag(f: float, cutoff: np.ndarray, order: int = 2) -> np.ndarray:
    """Magnitude of a Butterworth low-pass at frequency f (cutoff may be an array)."""
    return 1.0 / np.sqrt(1.0 + (f / np.maximum(cutoff, 1.0)) ** (2 * order))


def banded_stereo(
    n: int,
    sr: int,
    level_ctl: np.ndarray,
    cutoff_ctl: np.ndarray,
    seed: int,
    rho: float = 0.5,
    centers=OCTAVE_CENTERS,
    pan_ctl: np.ndarray | None = None,
) -> np.ndarray:
    """Noise whose RMS follows ``level_ctl`` (linear, control rate) and whose spectral tilt
    follows ``cutoff_ctl`` (Hz, control rate): a 2nd-order low-pass magnitude applied band by band,
    renormalised so the level stays exactly the requested one. Stereo, interchannel correlation
    ``rho``. ``pan_ctl`` (azimuth relative to the heading, deg) tilts the balance (gentle)."""
    rng = np.random.default_rng(seed)
    n1 = rng.standard_normal(n)
    n2 = rng.standard_normal(n)
    mags = np.array([lowpass_mag(f, cutoff_ctl) for f in centers])  # (B, nctl)
    norm = np.sqrt((mags**2).sum(axis=0))
    out = np.zeros((2, n))
    r2 = np.sqrt(1.0 - rho * rho)
    for k, f in enumerate(centers):
        g = ctl_to_audio(level_ctl * mags[k] / np.maximum(norm, 1e-12), n, sr)
        b1 = band(n1, sr, f)
        b2 = band(n2, sr, f)
        out[0] += g * b1
        out[1] += g * (rho * b1 + r2 * b2)
    # bands are independent, so the RMS of the sum is sqrt(sum of squares): that is what norm gave
    if pan_ctl is not None:
        bal = 0.35 * np.sin(np.radians(pan_ctl))
        bal = ctl_to_audio(bal, n, sr)
        gl, gr = 1.0 - bal, 1.0 + bal
        k = np.sqrt(2.0 / (gl**2 + gr**2))
        out[0] *= gl * k
        out[1] *= gr * k
    return out


def local_rms_normalise(x: np.ndarray, sr: int, window_s: float = 2.0, floor: float = 1e-4) -> np.ndarray:
    """Divide by the running RMS (so the level is set elsewhere); silent stretches stay silent."""
    w = max(3, int(window_s * sr))
    p = uniform_filter1d(x * x, w, mode="nearest")
    return x / np.sqrt(np.maximum(p, floor * floor))


def impulse_train(rng, n: int, rate_per_sample: np.ndarray, amp_sigma: float = 0.5):
    """Poisson event positions with a per-sample probability; returns (indices, amplitudes)."""
    u = rng.random(n)
    idx = np.nonzero(u < rate_per_sample)[0]
    amps = np.exp(amp_sigma * rng.standard_normal(len(idx)) - 0.5 * amp_sigma**2)
    return idx, amps


def convolve_events(n: int, idx: np.ndarray, amps: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    train = np.zeros(n)
    np.add.at(train, idx, amps)
    return fftconvolve(train, kernel)[:n]


def damped_sine(sr: int, f: float, tau_s: float, length_s: float | None = None, phase: float = 0.0) -> np.ndarray:
    length_s = length_s or 8 * tau_s
    t = np.arange(int(length_s * sr)) / sr
    return np.exp(-t / tau_s) * np.sin(2 * np.pi * f * t + phase)


def sum_kernels(*ks: np.ndarray) -> np.ndarray:
    """Sum kernels of different lengths (zero padded)."""
    m = max(len(k) for k in ks)
    out = np.zeros(m)
    for k in ks:
        out[: len(k)] += k
    return out

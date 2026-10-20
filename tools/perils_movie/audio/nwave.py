"""Acoustic building blocks of the thunder synthesis: the N-wave of one channel element, the ISO 9613-1 air
absorption as a minimum-phase filter, the low-frequency relaxation pulse, the scattering tail and the sparse
accumulation of pulses.

N-wave (Few 1970; Hill 1977; Ribner and Roy 1982): a short channel element radiates a shock wave that, away
from the channel, is an N: a compression, a linear fall through zero and a rarefaction. Its duration grows and
its rise (shock front) slows with distance (nonlinear propagation: weak shocks steepen the stronger they are,
so the front is sharpest close to the channel); both are modelled by power laws (``n_wave_params``), which are
an approximation to the measurements, not a fit.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import oaconvolve

from .acoustics import air_absorption_db_per_m


def n_wave_params(r_m: float) -> tuple[float, float]:
    """(rise time, duration) in seconds of the N-wave of an element r_m away: rise 0.05 ms at 200 m growing as
    r^0.8 (3 ms at most), duration 2 ms growing as (1 + r / 1.5 km)^0.6 (25 ms at most)."""
    rise = float(np.clip(0.0001 * (max(r_m, 1.0) / 200.0) ** 0.8, 0.00005, 0.003))
    dur = float(np.clip(0.002 * (1.0 + r_m / 1500.0) ** 0.6, 0.002, 0.025))
    return rise, dur


def n_wave(sr: int, r_m: float, dur_scale: float = 1.0) -> np.ndarray:
    """One N-wave sampled at ``sr``: rises to +1 over the rise time, falls linearly to -1 over the duration,
    returns to 0 over the rise time. Zero net area."""
    rise, dur = n_wave_params(r_m)
    dur *= dur_scale  # element-to-element variation: no two shocks have exactly the same length
    nr = max(1, round(rise * sr))
    nd = max(2, round(dur * sr))
    up = np.linspace(0.0, 1.0, nr, endpoint=False)
    mid = np.linspace(1.0, -1.0, nd)
    down = np.linspace(-1.0, 0.0, nr + 1)[
        1:
    ]  # same area as the rise: the N has no net impulse (no DC)
    w = np.concatenate([up, mid, down])
    w = np.convolve(w, [0.25, 0.5, 0.25], mode="same")  # the front is not a perfect step at 48 kHz
    return w


def min_phase_ir(mag: np.ndarray) -> np.ndarray:
    """Impulse response (length 2 (len(mag) - 1)) of the minimum-phase filter with the magnitude ``mag`` given on
    the rfft bins (homomorphic method): causal, so an arrival is not preceded by ringing."""
    n = 2 * (len(mag) - 1)
    c = np.fft.irfft(np.log(np.maximum(mag, 1e-12)), n)
    fold = np.zeros(n)
    fold[0] = 1.0
    fold[1 : n // 2] = 2.0
    fold[n // 2] = 1.0
    return np.fft.irfft(np.exp(np.fft.rfft(c * fold)), n)


def absorption_magnitude(
    f_hz: np.ndarray, r_m: float, temp_c: float, p_hpa: float, rh_pct: float
) -> np.ndarray:
    """Amplitude factor of the ISO 9613-1 air absorption over r_m metres at the frequencies f_hz."""
    alpha = air_absorption_db_per_m(np.maximum(f_hz, 1.0), temp_c, rh_pct, p_hpa)
    return 10.0 ** (-alpha * r_m / 20.0)


def absorbed(
    wave: np.ndarray,
    sr: int,
    r_m: float,
    temp_c: float,
    p_hpa: float,
    rh_pct: float,
    nfft: int = 16384,
) -> np.ndarray:
    """``wave`` after r_m metres of air (minimum-phase ISO 9613-1 filter), trimmed of its negligible tail."""
    nfft = max(nfft, 1 << int(np.ceil(np.log2(len(wave) + 2048))))
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    h = min_phase_ir(absorption_magnitude(f, r_m, temp_c, p_hpa, rh_pct))
    y = oaconvolve(wave, h[: nfft // 2])[: nfft // 2]
    big = np.nonzero(np.abs(y) > 1e-4 * max(np.abs(y).max(), 1e-30))[0]
    return y[: big[-1] + 1] if len(big) else y[:1]


def lf_pulse(sr: int, f0: float, tau: float = 0.06) -> np.ndarray:
    """Low-frequency pulse of the heated, relaxing channel: a damped oscillation at f0 (30 to 100 Hz) with a
    4 ms build-up, positive first."""
    t = np.arange(int(6 * tau * sr)) / sr
    return (1.0 - np.exp(-t / 0.004)) * np.exp(-t / tau) * np.sin(2 * np.pi * f0 * t)


def scatter_kernel(sr: int, rng, dur_s: float, density_hz: float = 300.0) -> np.ndarray:
    """Impulse response of the scattering by the ground and by atmospheric turbulence: sparse random arrivals of
    random sign whose amplitude decays exponentially (time constant dur / 3). Unit energy."""
    n = int(dur_s * sr)
    k = np.zeros(n)
    m = max(8, int(dur_s * density_hz))
    pos = np.minimum((rng.exponential(dur_s / 2.5, m) * sr).astype(int), n - 1)
    amp = rng.choice([-1.0, 1.0], m) * np.exp(-pos / sr / (dur_s / 3.0))
    np.add.at(k, pos, amp)
    s = np.sqrt((k * k).sum())
    return k / s if s > 0 else k


def accumulate(buf: np.ndarray, idx: np.ndarray, amp: np.ndarray, kernel: np.ndarray) -> None:
    """buf += sum_i amp_i * kernel shifted to idx_i (sparse sum; FFT overlap-add when there are very many)."""
    n, k = len(buf), len(kernel)
    ok = (idx > -k) & (idx < n)
    idx, amp = idx[ok], amp[ok]
    if len(idx) == 0:
        return
    if len(idx) > 3000:
        lo = max(0, int(idx.min()))
        hi = min(n, int(idx.max()) + k)
        train = np.bincount(np.clip(idx, lo, None) - lo, weights=amp, minlength=hi - lo + k)
        # an impulse at a negative index only contributes its tail: handled by the clip above (negligible)
        y = oaconvolve(train, kernel)[: hi - lo]
        buf[lo:hi] += y
        return
    for i, a in zip(idx.tolist(), amp.tolist(), strict=True):
        a0, b0 = max(i, 0), min(i + k, n)
        buf[a0:b0] += a * kernel[a0 - i : b0 - i]

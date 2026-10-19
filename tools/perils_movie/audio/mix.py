"""Mix and master: priority ducking, fades, ITU-R BS.1770 loudness, look-ahead limiter, true peak.

Priority (highest first): thunder > tornado > hail > rain > wind > convective / ambient. A layer is
ducked by the louder of the layers above it (a sidechain on the smoothed level), so the layers that
matter stay readable.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import minimum_filter1d, uniform_filter1d
from scipy.signal import lfilter, resample_poly

from .scene import CTL_RATE
from .wavio import read_wav, to_pcm, write_wav  # noqa: F401

PRIORITY = ["thunder", "tornado", "hail", "rain", "wind", "convective", "ambient"]
# by the ducking layer: (threshold dB re full scale of its 10 ms level, ratio, max reduction dB); a
# layer is only ducked when the one above is within 3 dB of it or louder (they compete)
DUCK = {
    "thunder": (-30.0, 0.5, 10.0),
    "tornado": (-32.0, 0.5, 6.0),
    "hail": (-36.0, 0.5, 4.0),
    "rain": (-42.0, 0.4, 3.0),
}

# K-weighting at 48 kHz (ITU-R BS.1770-4)
_K1 = (
    [1.53512485958697, -2.69169618940638, 1.19839281085285],
    [1.0, -1.69065929318241, 0.73248077421585],
)
_K2 = ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])


def k_weight(x: np.ndarray) -> np.ndarray:
    y = lfilter(*_K1, x, axis=-1)
    return lfilter(*_K2, y, axis=-1)


def lufs(x: np.ndarray, sr: int = 48000) -> float:
    """Integrated loudness (LUFS), BS.1770-4 with the -70 LUFS absolute and -10 LU relative gates."""
    if sr != 48000:
        raise ValueError("loudness is implemented for 48 kHz")
    x = np.atleast_2d(x)
    z = k_weight(x)
    blk, hop = int(0.4 * sr), int(0.1 * sr)
    if z.shape[1] < blk:
        return -120.0
    p = z * z
    c = np.concatenate([np.zeros((p.shape[0], 1)), np.cumsum(p, axis=1)], axis=1)
    starts = np.arange(0, z.shape[1] - blk + 1, hop)
    ms = (c[:, starts + blk] - c[:, starts]) / blk  # (ch, blocks)
    g = np.array([1.0, 1.0, 1.0, 1.41, 1.41])[: ms.shape[0]]
    z_j = (g[:, None] * ms).sum(axis=0)
    l_j = -0.691 + 10 * np.log10(np.maximum(z_j, 1e-20))
    a = l_j > -70.0
    if not a.any():
        return -120.0
    rel = -0.691 + 10 * np.log10(z_j[a].mean()) - 10.0
    gm = a & (l_j > rel)
    if not gm.any():
        return -120.0
    return float(-0.691 + 10 * np.log10(z_j[gm].mean()))


def true_peak_dbfs(x: np.ndarray, oversample: int = 4) -> float:
    y = resample_poly(np.atleast_2d(x), oversample, 1, axis=-1)
    return float(20 * np.log10(max(np.abs(y).max(), 1e-12)))


def level_env_db(x: np.ndarray, sr: int) -> np.ndarray:
    """10 ms RMS level (dB) at the control rate (100 Hz) of a stereo layer."""
    p = (x * x).mean(axis=0)
    hop = sr // CTL_RATE
    m = uniform_filter1d(p, hop, mode="nearest")[::hop]
    return 10 * np.log10(np.maximum(m, 1e-14))


def _smooth_gain(red_db: np.ndarray, attack_s=0.02, release_s=0.4) -> np.ndarray:
    a_att = np.exp(-1.0 / (attack_s * CTL_RATE))
    a_rel = np.exp(-1.0 / (release_s * CTL_RATE))
    out = np.empty_like(red_db)
    y = 0.0
    for i, r in enumerate(red_db):
        a = a_att if r > y else a_rel
        y = a * y + (1 - a) * r
        out[i] = y
    return out


def duck_layers(
    layers: dict[str, np.ndarray], sr: int
) -> tuple[dict[str, np.ndarray], dict[str, float], dict[str, float]]:
    """Apply the priority sidechain. Returns (ducked layers, max reduction in dB per layer)."""
    env = {k: level_env_db(v, sr) for k, v in layers.items()}
    out, maxred, meanred = {}, {}, {}
    n = next(iter(layers.values())).shape[1]
    for i, name in enumerate(PRIORITY):
        if name not in layers:
            continue
        red = np.zeros_like(env[name])
        for hi in PRIORITY[:i]:
            if hi not in layers or hi not in DUCK:
                continue
            thr, ratio, mx = DUCK[hi]
            r = np.clip((env[hi] - thr) * ratio, 0.0, mx)
            r = np.where(env[hi] > env[name] - 3.0, r, 0.0)
            red = np.maximum(red, r)
        red = _smooth_gain(red)
        g = 10 ** (-red / 20.0)
        ga = np.interp(np.arange(n) / sr, np.arange(len(g)) / CTL_RATE, g)
        out[name] = layers[name] * ga
        maxred[name] = float(red.max())
        meanred[name] = float(red.mean())
    return out, maxred, meanred


def fade_curve(n: int, sr: int, fade_in_s=2.0, fade_out_s=2.5) -> np.ndarray:
    g = np.ones(n)
    a, b = min(n, int(fade_in_s * sr)), min(n, int(fade_out_s * sr))
    if a > 1:
        g[:a] = 0.5 - 0.5 * np.cos(np.pi * np.arange(a) / a)
    if b > 1:
        g[-b:] *= 0.5 + 0.5 * np.cos(np.pi * np.arange(b) / b)
    return g


def limiter_gain(
    x: np.ndarray, sr: int, ceiling_db: float = -1.6, window_s: float = 0.01
) -> np.ndarray:
    """Look-ahead peak limiter as a gain curve (min filter, then an equal-length average: the
    smoothed gain can never exceed the requirement at any sample)."""
    ceil = 10 ** (ceiling_db / 20.0)
    pk = np.abs(x).max(axis=0)
    req = np.minimum(1.0, ceil / np.maximum(pk, 1e-12))
    w = int(window_s * sr) | 1
    return uniform_filter1d(minimum_filter1d(req, w, mode="nearest"), w, mode="nearest")


def master(
    layers: dict[str, np.ndarray],
    sr: int,
    target_lufs=-23.0,
    ceiling_db=-1.6,
    tp_max_db=-1.0,
    fade_in_s=2.0,
    fade_out_s=2.5,
):
    """Duck, sum, fade, normalise to the target loudness, limit. Returns (stereo, info, stems)."""
    ducked, maxred, meanred = duck_layers(layers, sr)
    n = next(iter(layers.values())).shape[1]
    fade = fade_curve(n, sr, fade_in_s, fade_out_s)
    pre = {k: v * fade for k, v in ducked.items()}
    mix = sum(pre.values())
    loud0 = lufs(mix, sr)
    gain = 10 ** ((target_lufs - loud0) / 20.0) if loud0 > -100 else 1.0
    lim = np.ones(n)
    y = mix
    for _ in range(4):
        y0 = mix * gain
        lim = limiter_gain(y0, sr, ceiling_db)
        y = y0 * lim
        l1 = lufs(y, sr)
        if abs(l1 - target_lufs) < 0.1 or l1 < -100:
            break
        gain *= 10 ** ((target_lufs - l1) / 20.0)
    tp = true_peak_dbfs(y)
    trim_db = 0.0
    if tp > tp_max_db:
        trim_db = tp_max_db - tp - 0.05
        y = y * 10 ** (trim_db / 20.0)
        gain *= 10 ** (trim_db / 20.0)
    stems = {k: v * gain * lim for k, v in pre.items()}
    mono = y.mean(axis=0)
    st_rms = float(np.sqrt(np.mean(y**2)))
    corr = float(np.corrcoef(y[0], y[1])[0, 1]) if y.std() > 0 else 1.0
    info = {
        "lufs_before_master": loud0,
        "master_gain_db": float(20 * np.log10(gain)),
        "lufs_integrated": lufs(y, sr),
        "true_peak_dbfs": true_peak_dbfs(y),
        "sample_peak_dbfs": float(20 * np.log10(max(np.abs(y).max(), 1e-12))),
        "limiter_max_reduction_db": float(-20 * np.log10(max(lim.min(), 1e-6))),
        "clipped_samples": int((np.abs(y) >= 0.999).sum()),
        "ducking_max_reduction_db": maxred,
        "ducking_mean_reduction_db": meanred,
        "mono_compat": {
            "stereo_rms_dbfs": float(20 * np.log10(max(st_rms, 1e-12))),
            "mono_sum_rms_dbfs": float(20 * np.log10(max(np.sqrt(np.mean(mono**2)), 1e-12))),
            "mono_loss_db": float(
                20 * np.log10(max(st_rms, 1e-12) / max(np.sqrt(np.mean(mono**2)), 1e-12))
            ),
            "lr_correlation": corr,
        },
    }
    return y, info, stems

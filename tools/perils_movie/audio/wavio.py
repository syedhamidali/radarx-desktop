"""WAV in and out (numpy and the standard library only, so the mux can run in a minimal environment)."""

from __future__ import annotations

import wave

import numpy as np


def to_pcm(y: np.ndarray, bits: int, seed: int = 0) -> np.ndarray:
    """(N, 2) integer PCM (y is (N, 2)); 16-bit gets seeded TPDF dither."""
    if bits == 16:
        rng = np.random.default_rng(seed)
        d = rng.random(y.shape) - rng.random(y.shape)
        q = np.round(y * 32767.0 + d)
        return np.clip(q, -32768, 32767).astype("<i2")
    q = np.round(y * 8388607.0)
    return np.clip(q, -8388608, 8388607).astype("<i4")


def write_wav(path, y: np.ndarray, sr: int, bits: int = 24, seed: int = 0) -> None:
    pcm = to_pcm(y, bits, seed)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(bits // 8)
        w.setframerate(sr)
        if bits == 16:
            w.writeframes(pcm.tobytes())
        else:  # pack 24-bit little-endian from int32
            b = np.ascontiguousarray(pcm.astype("<i4")).view(np.uint8).reshape(-1, 4)[:, :3]
            w.writeframes(b.tobytes())


def read_wav(path) -> tuple[np.ndarray, int]:
    """(2, N) float array in [-1, 1) and the sample rate (16 or 24 bit PCM)."""
    with wave.open(str(path), "rb") as w:
        ch, sw, sr, nf = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(nf)
    if sw == 2:
        a = np.frombuffer(raw, "<i2").astype(np.float64) / 32768.0
    elif sw == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        v = b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16)
        v = np.where(v & 0x800000, v - (1 << 24), v)
        a = v.astype(np.float64) / 8388608.0
    else:
        raise ValueError(f"unsupported sample width {sw}")
    return a.reshape(-1, ch).T, sr

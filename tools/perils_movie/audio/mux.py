"""Mux the WAV soundtrack into the MP4 as AAC (+faststart) with PyAV, copying the video stream
untouched (same packets: frame count and duration are unchanged).

Needs PyAV and numpy only (run it from a scratch environment outside the repository, like the clip
renderers: ``python -m venv /tmp/clipenv && /tmp/clipenv/bin/pip install av numpy``).
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np

from .wavio import read_wav


def _probe(path) -> dict:
    import av

    with av.open(str(path)) as c:
        v = c.streams.video[0]
        n = sum(1 for p in c.demux(v) if p.dts is not None and p.size > 0)
    with av.open(str(path)) as c:
        v = c.streams.video[0]
        dur = float(c.duration) / 1e6 if c.duration else None
        a = c.streams.audio[0] if c.streams.audio else None
        return {
            "video_packets": n,
            "duration_s": dur,
            "fps": float(v.average_rate) if v.average_rate else None,
            "audio": None if a is None else {"codec": a.codec_context.name, "rate": a.rate,
                                             "channels": a.channels},
        }


def mux_audio(video_in, wav, video_out, bitrate: int = 192_000) -> dict:
    """Write ``video_out`` = the video of ``video_in`` (copied, not re-encoded) + the WAV as AAC.

    The audio is trimmed or zero-padded to the video duration. Any audio already in the input is
    dropped. Returns frame counts and durations of the input and the output."""
    import av

    pcm, sr = read_wav(wav)
    before = _probe(video_in)
    src = av.open(str(video_in))
    vs = src.streams.video[0]
    vdur = float(src.duration) / 1e6 if src.duration else float(vs.duration * vs.time_base)
    n_aud = round(vdur * sr)
    if pcm.shape[1] >= n_aud:
        pcm = pcm[:, :n_aud]
    else:
        pcm = np.pad(pcm, ((0, 0), (0, n_aud - pcm.shape[1])))
    planar = np.ascontiguousarray(pcm.astype(np.float32))

    out_path = Path(video_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dst = av.open(str(out_path), "w", options={"movflags": "+faststart"})
    ovs = dst.add_stream_from_template(vs)
    oas = dst.add_stream("aac", rate=sr)
    oas.layout = "stereo"
    oas.bit_rate = bitrate

    apk = []
    for i0 in range(0, planar.shape[1], 1024):
        fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(planar[:, i0 : i0 + 1024]), format="fltp",
                                        layout="stereo")
        fr.sample_rate = sr
        fr.pts = i0
        fr.time_base = Fraction(1, sr)
        apk.extend(oas.encode(fr))
    apk.extend(oas.encode())

    def t_of(p):
        return float(p.dts * p.time_base) if p.dts is not None else 0.0

    for pkt in src.demux(vs):
        if pkt.dts is None:
            continue
        while apk and t_of(apk[0]) <= t_of(pkt):
            dst.mux(apk.pop(0))
        pkt.stream = ovs
        dst.mux(pkt)
    for p in apk:
        dst.mux(p)
    dst.close()
    src.close()
    after = _probe(out_path)
    return {"before": before, "after": after, "audio_samples": int(planar.shape[1]), "sample_rate": sr,
            "frames_kept": before["video_packets"] == after["video_packets"]}

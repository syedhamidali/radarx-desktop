"""Look at the thunder: spectrograms, envelopes and numbers of a few voiced events, optionally of several
renders side by side (old vs new). Works on a thunder-only stem and its report.

    python/.venv/bin/python tools/perils_movie/audio/verify_thunder.py --out plots \
        --render v3=stem_v3.wav:report_v3.json --render v4=stem_v4.wav:report_v4.json

Checked against the literature on thunder acoustics (Few 1970; Holmes et al. 1971; Hill 1977; Ribner and Roy
1982; Bass 1980): a sharp onset (the nearest point of the channel), a close channel with energy at 1 to 5 kHz,
a low rumble with its spectral peak at 20 to 100 Hz, the highs removed with distance (ISO 9613-1), a duration
that grows with the distance and the channel length, an irregular decaying tail.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _here]
sys.path.insert(0, str(_here.parents[1]))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.signal import spectrogram

from perils_movie.audio.wavio import read_wav


def event_metrics(x: np.ndarray, sr: int, t0: float, span_s: float = 25.0) -> dict:
    """Numbers of one event: x (2, N) thunder stem, t0 the first arrival (clip s)."""
    a = max(0, int((t0 - 0.3) * sr))
    z = min(x.shape[1], int((t0 + span_s) * sr))
    m = x[:, a:z].mean(axis=0)
    env = np.sqrt(np.convolve(m * m, np.ones(sr // 50) / (sr // 50), mode="same"))
    pk = float(env.max())
    ipk = int(env.argmax())
    above = np.nonzero(env > 10 ** (-30 / 20) * pk)[0]
    i10 = int(np.argmax(env > 0.1 * pk))
    i90 = int(np.argmax(env > 0.9 * pk))
    spec = np.abs(np.fft.rfft(m * np.hanning(len(m)))) ** 2
    f = np.fft.rfftfreq(len(m), 1 / sr)
    band = (f >= 15) & (f <= 2000)
    tot = float(spec.sum()) or 1.0
    return {
        "peak_dbfs": float(20 * np.log10(max(np.abs(m).max(), 1e-12))),
        "centroid_hz": float((spec * f).sum() / tot),
        "peak_freq_hz": float(f[band][np.argmax(spec[band])]),
        "frac_energy_above_1k": float(spec[f >= 1000].sum() / tot),
        "frac_energy_below_150": float(spec[f <= 150].sum() / tot),
        "duration_30db_s": float((above[-1] - above[0]) / sr) if len(above) else 0.0,
        "onset_to_peak_s": float(max(ipk, 0) / sr),
        "rise_10_90_ms": float(max(i90 - i10, 0) / sr * 1000),
    }


def pick_events(rep: dict, n_each: int = 1):
    """One near (< 10 km), one mid (10 to 30) and one far (> 30 km) voiced event, the largest by sources."""
    rows = [r for r in rep["flashes"] if r.get("status") == "rendered"]
    out = []
    for lo, hi, name in ((0, 10, "near"), (10, 30, "mid"), (30, 500, "far")):
        c = [r for r in rows if lo <= r["dist_nearest_m"] / 1000 < hi]
        c.sort(key=lambda r: -r["n_sources"])
        out += [(name, r) for r in c[:n_each]]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--render", action="append", required=True, help="label=stem.wav:report.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ids", help="comma separated flash ids to show (same in every render)")
    a = ap.parse_args(argv)
    renders = []
    for spec in a.render:
        label, rest = spec.split("=", 1)
        wav, rep = rest.split(":", 1)
        x, sr = read_wav(wav)
        r = json.loads(Path(rep).read_text())
        renders.append((label, x, sr, r.get("thunder", r)))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    # events: from the first render's report (or the ids given)
    rep0 = renders[0][3]
    if a.ids:
        want = set(a.ids.split(","))
        ev = [("sel", r) for r in rep0["flashes"] if str(r["id"]) in want]
    else:
        ev = pick_events(rep0)
    fig, ax = plt.subplots(
        len(ev),
        2 * len(renders),
        figsize=(7 * len(renders), 3.1 * len(ev)),
        squeeze=False,
        constrained_layout=True,
    )
    print(
        f"{'event':28s} {'render':6s} "
        + " ".join(
            f"{k:>14s}"
            for k in (
                "centroid_hz",
                "peak_freq_hz",
                "frac>1k",
                "frac<150",
                "dur30dB_s",
                "rise10-90_ms",
                "peak_dbfs",
            )
        )
    )
    for i, (name, row) in enumerate(ev):
        for j, (label, x, sr, rep) in enumerate(renders):
            r = next((q for q in rep["flashes"] if str(q["id"]) == str(row["id"])), None)
            if r is None or r.get("status") != "rendered":
                ax[i][2 * j].set_title(f"{label}: flash {row['id']} not voiced")
                continue
            t0 = r["first_arrival_clip_s"]
            m = event_metrics(x, sr, t0)
            print(
                f"{name:5s} id {row['id']!s:6s} {row['dist_nearest_m'] / 1000:5.1f} km  {label:6s} "
                f"{m['centroid_hz']:14.0f} {m['peak_freq_hz']:14.0f} {m['frac_energy_above_1k']:14.3f} {m['frac_energy_below_150']:14.3f} "
                f"{m['duration_30db_s']:14.1f} {m['rise_10_90_ms']:14.1f} {m['peak_dbfs']:14.1f}"
            )
            s0, s1 = max(0, int((t0 - 0.5) * sr)), min(x.shape[1], int((t0 + 25.0) * sr))
            seg = x[:, s0:s1].mean(axis=0)
            f, tt, S = spectrogram(seg, sr, nperseg=2048, noverlap=1792)
            axs = ax[i][2 * j]
            axs.pcolormesh(
                tt - 0.5,
                f,
                10 * np.log10(S + 1e-16),
                shading="auto",
                vmin=-150,
                vmax=-60,
                cmap="magma",
            )
            axs.set_yscale("symlog", linthresh=100)
            axs.set_ylim(15, 12000)
            axs.set_title(
                f"{label}: flash {row['id']} ({name}, {row['dist_nearest_m'] / 1000:.1f} km, {row['n_sources']} sources)",
                fontsize=9,
            )
            axs.set_ylabel("Hz")
            axe = ax[i][2 * j + 1]
            env = np.sqrt(np.convolve(seg * seg, np.ones(sr // 50) / (sr // 50), mode="same"))
            axe.plot(np.arange(len(seg)) / sr - 0.5, 20 * np.log10(env + 1e-9), lw=0.8)
            axe.set_ylim(-110, 0)
            axe.set_title("envelope (dBFS, 20 ms)", fontsize=9)
    for j in range(len(renders)):
        ax[-1][2 * j].set_xlabel("s after the first arrival")
        ax[-1][2 * j + 1].set_xlabel("s after the first arrival")
    p = out / "thunder_compare.png"
    fig.savefig(p, dpi=100)
    print("plot", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

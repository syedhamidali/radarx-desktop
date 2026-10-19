"""Verification plots and numbers of a rendered soundtrack (to LOOK at): waveform, spectrogram,
per-layer levels, the flash / delay table, wind and rain against the clip.

    python/.venv/bin/python tools/perils_movie/audio/verify_audio.py \
        --wav perils_audio.wav --report audio_report.json --out plots/

(matplotlib, numpy, scipy; the repository venv has them.) With ``--render`` it renders first from
``--bundle --timeline --camera`` (same options as make_audio.py).
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

COL = {"thunder": "#d62728", "tornado": "#8c564b", "hail": "#9467bd", "rain": "#1f77b4",
       "wind": "#2ca02c", "convective": "#ff7f0e", "ambient": "#7f7f7f"}


def plots(wav, report, out_dir) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    x, sr = read_wav(wav)
    rep = json.loads(Path(report).read_text()) if not isinstance(report, dict) else report
    t = np.arange(x.shape[1]) / sr
    paths = []

    fig, ax = plt.subplots(3, 1, figsize=(13, 8.5), sharex=True, constrained_layout=True)
    step = max(1, sr // 200)
    ax[0].plot(t[::step], x[0, ::step], lw=0.4, color="#1f77b4", label="left")
    ax[0].plot(t[::step], x[1, ::step], lw=0.4, color="#d62728", alpha=0.7, label="right")
    ax[0].set_ylabel("amplitude")
    ax[0].set_title(f"{rep['label']}: waveform, spectrogram, thunder arrivals "
                    f"({rep['master']['lufs_integrated']:.1f} LUFS, true peak {rep['master']['true_peak_dbfs']:.1f} dBFS)")
    ax[0].legend(loc="upper right", ncol=2)
    f, tt, S = spectrogram(x.mean(axis=0), sr, nperseg=4096, noverlap=3072)
    ax[1].pcolormesh(tt, f, 10 * np.log10(S + 1e-14), shading="auto", vmin=-130, vmax=-50, cmap="magma")
    ax[1].set_yscale("symlog", linthresh=200)
    ax[1].set_ylim(20, 16000)
    ax[1].set_ylabel("Hz")
    fl = [r for r in rep["thunder"]["flashes"] if r.get("status") == "rendered"]
    for r in fl:
        ax[1].axvline(r["first_arrival_clip_s"], color="white", lw=0.4, alpha=0.5)
    for r in fl:
        ax[2].vlines(r["first_arrival_clip_s"], 0, r["dist_nearest_m"] / 1000, color=COL["thunder"], lw=1.2)
    ax[2].set_ylabel("nearest source (km)")
    ax[2].set_xlabel("clip time (s)")
    p = out_dir / "waveform_spectrogram.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    paths.append(p)

    fig, ax = plt.subplots(figsize=(12, 5), constrained_layout=True)
    for k, v in rep["layers"].items():
        lv = np.array(v["levels_dbfs_per_s"])
        ax.plot(np.arange(len(lv)) + 0.5, np.maximum(lv, -100), color=COL.get(k, "k"), label=k, lw=1.5)
    ax.set_ylim(-90, 0)
    ax.set_xlabel("clip time (s)")
    ax.set_ylabel("level, 1 s RMS (dBFS, in the final mix)")
    ax.set_title("Per-layer levels (priority: " + " > ".join(rep["priority"][:5]) + ")")
    ax.legend(ncol=4)
    p = out_dir / "layer_levels.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    paths.append(p)

    allf = [r for r in rep["thunder"]["flashes"] if "dist_nearest_m" in r]
    fig, ax = plt.subplots(1, 2, figsize=(13, 5), constrained_layout=True)
    if allf:
        d = np.array([r["dist_nearest_m"] / 1000 for r in allf])
        dl = np.array([r["delay_nearest_clip_s"] for r in allf])
        st = [r["status"] for r in allf]
        for s, c in (("rendered", COL["thunder"]), ("skipped_density", "#999999"), ("skipped_masked", "#cccccc"),
                     ("skipped_out_of_range", "#000000"), ("outside_clip", "#bbbb00")):
            m = np.array([x == s for x in st])
            if m.any():
                ax[0].scatter(d[m], dl[m], s=14, color=c, label=f"{s} ({m.sum()})")
        sp = rep["params"]["speedup"]
        if isinstance(sp, int | float):
            xs = np.linspace(0, max(d.max(), 1), 50)
            ax[0].plot(xs, xs * 1000 / 343.0 / sp, "k--", lw=0.8, label=f"distance / 343 m/s / {sp:g}")
        ax[0].set_xlabel("distance to the nearest LMA source (km)")
        ax[0].set_ylabel("thunder delay in the clip (s)")
        ax[0].legend(fontsize=8)
        ax[1].hist(d, bins=20, color="#888888")
        ax[1].set_xlabel("distance (km)")
        ax[1].set_ylabel("flashes")
    p = out_dir / "flash_delay_table.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    paths.append(p)
    return paths


def table(rep, n=30) -> str:
    rows = [r for r in rep["thunder"]["flashes"] if r.get("status") == "rendered"][:n]
    lines = ["  id          t_flash  first_arr  nearest_km  far_km  delay_clip_s  az_rel  level_dB  n_src"]
    for r in rows:
        lines.append(
            f"  {str(r['id'])[:10]:10s} {r['t_flash_clip_s']:7.2f}  {r['first_arrival_clip_s']:9.2f}  "
            f"{r['dist_nearest_m'] / 1000:9.2f}  {r['dist_farthest_m'] / 1000:6.1f}  "
            f"{r['delay_nearest_clip_s']:11.2f}  {r['az_rel_nearest_deg']:6.0f}  {r['peak_dbfs_premaster']:8.1f}  "
            f"{r['n_sources']:5d}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav")
    ap.add_argument("--report")
    ap.add_argument("--out", required=True, help="folder for the plots")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--bundle")
    ap.add_argument("--timeline")
    ap.add_argument("--camera")
    ap.add_argument("--events")
    a = ap.parse_args(argv)
    if a.render:
        from perils_movie.audio.render import render_audio

        a.wav = a.wav or str(Path(a.out) / "audio.wav")
        a.report = a.report or str(Path(a.out) / "audio_report.json")
        render_audio(a.bundle, a.timeline, a.camera, a.wav, report_path=a.report, events=a.events)
    rep = json.loads(Path(a.report).read_text())
    for p in plots(a.wav, rep, a.out):
        print("plot", p)
    m = rep["master"]
    print(f"loudness {m['lufs_integrated']:.2f} LUFS, true peak {m['true_peak_dbfs']:.2f} dBFS, "
          f"clipped {m['clipped_samples']}, mono loss {m['mono_compat']['mono_loss_db']:.2f} dB")
    print("thunder:", rep["thunder"]["counts"])
    for k, v in rep["layers"].items():
        print(f"  {k:10s} rms {v['rms_dbfs']:7.1f} dBFS  peak {v['peak_dbfs']:6.1f}  duck max {v['max_ducking_db']:.1f} dB")
    print(table(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

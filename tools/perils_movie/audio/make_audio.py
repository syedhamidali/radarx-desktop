"""Command line: render the PERiLS soundtrack and/or mux it into a video.

    # render (repository venv: numpy + scipy)
    python/.venv/bin/python tools/perils_movie/audio/make_audio.py \
        --bundle ~/Downloads/xradar-exports/perils_movie/bundle \
        --timeline ~/Downloads/xradar-exports/perils_movie/timeline.json \
        --camera ~/Downloads/xradar-exports/perils_movie/camera_path.json \
        --out ~/Downloads/xradar-exports/perils_movie/audio/perils_audio.wav

    # mux (scratch venv with PyAV, outside the repository)
    /tmp/clipenv/bin/python tools/perils_movie/audio/make_audio.py --mux-only \
        --wav perils_audio.wav --video-in silent.mp4 --video-out perils_movie.mp4

    # a synthetic demo (never the real data)
    python/.venv/bin/python tools/perils_movie/audio/make_audio.py --synthetic /tmp/syn --out /tmp/syn/a.wav

Everything is synthesised from the data, not a recording.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# the script's own folder holds modules (timeline.py, mix.py ...): keep them out of the import path
_here = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _here]
sys.path.insert(0, str(_here.parents[1]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--bundle", help="bundle folder (manifest.json, lma/, stations/, ...)")
    ap.add_argument("--timeline", help="timeline.json of the renderer")
    ap.add_argument("--camera", help="camera_path.json of the renderer")
    ap.add_argument("--events", help="events.json of the renderer (tornado / hail / flash events)")
    ap.add_argument("--out", help="output WAV (48 kHz stereo)")
    ap.add_argument("--report", help="audio_report.json (default: next to the WAV)")
    ap.add_argument("--bits", type=int, default=24, choices=(16, 24))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--target-lufs", type=float, default=-23.0)
    ap.add_argument(
        "--max-listener-height",
        type=float,
        default=500.0,
        help="the listener is the camera's ground track, at most this high above the ground (m; 0 = the camera's own altitude)",
    )
    ap.add_argument("--stems", help="also write one WAV per layer into this folder")
    ap.add_argument(
        "--synthetic", metavar="DIR", help="write and use a SYNTHETIC demo bundle in DIR"
    )
    ap.add_argument("--video-in", help="silent video to mux the audio into")
    ap.add_argument("--video-out", help="output MP4 with the AAC track")
    ap.add_argument("--wav", help="WAV to mux (default: --out)")
    ap.add_argument("--mux-only", action="store_true", help="only mux (needs PyAV)")
    a = ap.parse_args(argv)

    wav = a.wav or a.out
    if not a.mux_only:
        from perils_movie.audio.render import render_audio

        if a.synthetic:
            from perils_movie.audio.synthetic import write_synthetic_bundle

            d = write_synthetic_bundle(Path(a.synthetic) / "bundle")
            a.bundle = str(d)
            a.timeline = str(d.parent / "timeline.json")
            a.camera = str(d.parent / "camera_path.json")
        if not (a.bundle and a.timeline and a.camera and a.out):
            ap.error(
                "--bundle, --timeline, --camera and --out are required (or --synthetic DIR and --out)"
            )
        rep = render_audio(
            a.bundle,
            a.timeline,
            a.camera,
            a.out,
            report_path=a.report,
            events=a.events,
            bits=a.bits,
            seed=a.seed,
            target_lufs=a.target_lufs,
            stems_dir=a.stems,
            max_listener_height_m=a.max_listener_height or None,
        )
        m = rep["master"]
        print(
            f"wrote {a.out}: {rep['output']['duration_s']:.1f} s, {m['lufs_integrated']:.1f} LUFS, "
            f"true peak {m['true_peak_dbfs']:.1f} dBFS, thunder {rep['thunder']['counts']}"
        )
    if a.video_in and a.video_out:
        from perils_movie.audio.mux import mux_audio

        if not wav:
            ap.error("--wav (or --out) is needed to mux")
        res = mux_audio(a.video_in, wav, a.video_out)
        print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

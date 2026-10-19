"""Tests of the PERiLS soundtrack (tools/perils_movie/audio): thunder timing and physics, the rain /
wind / tornado / hail layers, the mix, determinism and the AAC mux. Synthetic data only."""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from perils_movie.audio import layers as ly
from perils_movie.audio import mix as mx
from perils_movie.audio.acoustics import (
    SPEED_OF_SOUND_MS,
    air_absorption_db_per_m,
    binaural,
    spectral_centroid,
    thunder_delay_s,
)
from perils_movie.audio.bundle import (
    Bundle,
    Event,
    Flash,
    Station,
    TornadoTrack,
    parse_events,
)
from perils_movie.audio.render import render_mix
from perils_movie.audio.scene import Scene
from perils_movie.audio.synthetic import LAT0, LON0, WINDOW_START, offset_ll, synthetic_scenario
from perils_movie.audio.thunder import ThunderParams, plan_flash, render_flash, select_flashes
from perils_movie.audio.timeline import CameraPath, Timeline
from perils_movie.audio.wavio import read_wav, write_wav

SR = 48000


def cam(heading=0.0, dur=60.0):
    return CameraPath([
        {"t_clip_s": 0.0, "lat": LAT0, "lon": LON0, "alt_m": 0.0, "heading_deg": heading},
        {"t_clip_s": dur, "lat": LAT0, "lon": LON0, "alt_m": 0.0, "heading_deg": heading},
    ])


def flash_at(east, north, alt=3000.0, t=10.0, fid="f", n=1, power=5.0):
    la, lo = offset_ll(LAT0, LON0, east, north)
    src = np.array([[t + 0.01 * k, la, lo, alt, power] for k in range(n)], float)
    return Flash(fid, t, t + 0.01 * n, (la, lo, alt), src, n)


def station(rain=None, dm=None, wind=None, de=0.0, dn=0.0, events=None, wdir=270.0, dbz=None, sid="S"):
    la, lo = offset_ll(LAT0, LON0, de, dn)
    t = np.array([0.0, 100000.0])
    ser = {}
    for k, v in (("rain_rate_mmh", rain), ("dm_mm", dm), ("wind_speed_ms", wind), ("dbz", dbz)):
        if v is not None:
            ser[k] = (t, np.array([v, v], float))
    if wind is not None:
        ser["wind_dir_deg"] = (t, np.array([wdir, wdir], float))
    return Station(sid, sid, "test", la, lo, 0.0, ser, events or [])


def bundle(stations=(), flashes=(), tornadoes=(), events=()):
    return Bundle(WINDOW_START, None, list(flashes), list(stations), list(tornadoes), list(events))


def scene(b, dur=10.0, speedup=5.0, heading=0.0, events=()):
    return Scene(b, Timeline.constant(dur, speedup), cam(heading, dur), dur, list(events))


def tor_track(east, north, t0=-1.0, t1=1e5, sid="T"):
    la, lo = offset_ll(LAT0, LON0, east, north)
    return TornadoTrack(sid, sid, "EF2", np.array([t0, t1]), np.array([la, la]), np.array([lo, lo]))


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x) ** 2)))


# ----------------------------------------------------------------------------- thunder


@pytest.mark.parametrize("speedup", [1.0, 5.0, 12.5])
def test_thunder_delay_is_distance_over_343_over_speedup_exactly(speedup):
    tl = Timeline.constant(120.0, speedup)
    f = flash_at(east=3000.0, north=4000.0, alt=0.0, t=100.0 * speedup / 5.0)
    row = plan_flash(f, tl, cam(0.0, 120.0), 120.0, ThunderParams())
    d = row["dist_nearest_m"]
    assert d == pytest.approx(5000.0, rel=2e-3)
    assert row["delay_nearest_clip_s"] == pytest.approx(d / SPEED_OF_SOUND_MS / speedup, abs=1e-9)
    assert row["delay_nearest_clip_s"] == pytest.approx(float(thunder_delay_s(d, speedup)), abs=1e-9)
    t_flash = float(tl.data_to_clip(f.t_start_s))
    assert row["first_arrival_clip_s"] == pytest.approx(t_flash + d / SPEED_OF_SOUND_MS / speedup, abs=1e-9)


def test_thunder_delay_follows_a_varying_speedup():
    tl = Timeline.from_dict({"clip_duration_s": 120, "speedup": [{"t_clip_s": 0, "speedup": 2.0},
                                                                  {"t_clip_s": 120, "speedup": 10.0}]})
    f = flash_at(0.0, 6860.0, alt=0.0, t=float(tl.clip_to_data(40.0)))
    row = plan_flash(f, tl, cam(0.0, 120.0), 120.0, ThunderParams())
    # sound arrives at data time t + r/343: the clip delay is that, mapped through the clock
    expect = float(tl.data_to_clip(f.t_start_s + row["dist_nearest_m"] / SPEED_OF_SOUND_MS)) - 40.0
    assert row["delay_nearest_clip_s"] == pytest.approx(expect, abs=1e-6)
    assert 20.0 / 12.0 < row["delay_nearest_clip_s"] < 20.0 / 4.0


def test_audio_is_silent_before_the_arrival_and_loud_after():
    tl = Timeline.constant(30.0, 5.0)
    f = flash_at(0.0, 3430.0, alt=0.0, t=50.0)  # clip 10 s, +10 s real /5 = 2 s -> arrives at 12 s
    b = bundle(flashes=[f])
    _y, rep, stems = render_mix(b, tl, cam(0.0, 30.0), layers={"thunder"})
    th = stems["thunder"]
    arr = rep["thunder"]["flashes"][0]["first_arrival_clip_s"]
    assert arr == pytest.approx(10.0 + 2.0, abs=0.01)
    assert rms(th[:, : int((arr - 0.3) * SR)]) < 1e-4 * rms(th[:, int(arr * SR): int((arr + 1.5) * SR)])
    assert rms(th[:, int(arr * SR): int((arr + 1.5) * SR)]) > 1e-3


def test_a_long_flash_starts_at_its_nearest_part_and_rolls_on():
    tl = Timeline.constant(30.0, 5.0)
    la0, lo0 = offset_ll(LAT0, LON0, 0.0, 3000.0)
    la1, _lo1 = offset_ll(LAT0, LON0, 0.0, 13000.0)
    t = np.linspace(50.0, 51.0, 20)
    lat = np.linspace(la0, la1, 20)
    src = np.column_stack([t, lat, np.full(20, lo0), np.zeros(20), np.zeros(20)])
    f = Flash("long", 50.0, 51.0, (lat.mean(), lo0, 0.0), src, 20)
    row = plan_flash(f, tl, cam(0.0, 30.0), 30.0, ThunderParams())
    assert row["dist_farthest_m"] > row["dist_nearest_m"] + 9000
    spread = row["last_arrival_clip_s"] - row["first_arrival_clip_s"]
    assert spread == pytest.approx(10000.0 / SPEED_OF_SOUND_MS / 5.0, rel=0.15)


def test_farther_flash_is_later_quieter_and_lower_passed():
    tl = Timeline.constant(60.0, 5.0)
    prm = ThunderParams()
    rows, audio = [], []
    for d in (2500.0, 16000.0):
        f = flash_at(0.0, d, alt=0.0, t=50.0, n=40)
        row = plan_flash(f, tl, cam(0.0, 60.0), 60.0, prm)
        select_flashes([row], prm)
        _start, x = render_flash(row, SR, 1, cam(0.0, 60.0), prm, (15.0, 1000.0))
        rows.append(row)
        audio.append(x.mean(axis=0))
    near, far = rows
    assert far["first_arrival_clip_s"] > near["first_arrival_clip_s"]
    assert far["peak_amp"] < near["peak_amp"]
    assert spectral_centroid(audio[1], SR) < spectral_centroid(audio[0], SR)


def test_level_follows_one_over_r_with_a_minimum_range():
    from perils_movie.audio.acoustics import distance_gain

    a1, _ = distance_gain(1000.0)
    a2, _ = distance_gain(2000.0)
    assert a1 / a2 == pytest.approx(2.0)
    a_close, _ = distance_gain(10.0)
    a_r0, _ = distance_gain(300.0)
    assert a_close == a_r0  # no singularity overhead


def test_pan_sign_matches_the_azimuth():
    mono = np.random.default_rng(0).standard_normal(SR // 2)
    left = binaural(mono, SR, -60.0)
    right = binaural(mono, SR, +60.0)
    assert rms(left[0]) > rms(left[1])
    assert rms(right[1]) > rms(right[0])
    ctr = binaural(mono, SR, 0.0)
    assert rms(ctr[0]) == pytest.approx(rms(ctr[1]), rel=1e-6)
    # the ear on the far side also hears the sound later (interaural time difference)
    lag = np.argmax(np.correlate(left[0][:4000], left[1][:4000], "full")) - 3999
    assert lag < 0  # the left ear leads


@pytest.mark.parametrize(
    ("heading", "east", "north", "louder"),
    [(0.0, -8000.0, 0.0, 0), (0.0, 8000.0, 0.0, 1), (90.0, 0.0, 8000.0, 0), (90.0, 0.0, -8000.0, 1)],
)
def test_flash_to_the_left_of_the_heading_is_louder_in_the_left_channel(heading, east, north, louder):
    tl = Timeline.constant(30.0, 5.0)
    b = bundle(flashes=[flash_at(east, north, alt=0.0, t=50.0, n=30)])
    _, rep, stems = render_mix(b, tl, cam(heading, 30.0), layers={"thunder"})
    th = stems["thunder"]
    assert rms(th[louder]) > 1.2 * rms(th[1 - louder])
    assert rep["thunder"]["flashes"][0]["az_rel_nearest_deg"] == pytest.approx(-90.0 if louder == 0 else 90.0, abs=1.0)


def test_dense_bursts_are_thinned_and_counted():
    tl = Timeline.constant(30.0, 5.0)
    prm = ThunderParams()
    ang = np.radians(np.arange(12) * 30.0)
    flashes = [flash_at(5000.0 * np.sin(a), 5000.0 * np.cos(a), alt=0.0, t=50.0, fid=f"f{i}")
               for i, a in enumerate(ang)]
    rows = [plan_flash(f, tl, cam(0.0, 30.0), 30.0, prm) for f in flashes]
    counts = select_flashes(rows, prm)
    assert counts["rendered"] <= prm.window_cap * 2
    assert sum(counts.values()) == 12
    assert counts.get("skipped_density", 0) + counts.get("skipped_masked", 0) == 12 - counts["rendered"]


def test_air_absorption_is_iso_9613_like():
    a = air_absorption_db_per_m(np.array([125.0, 1000.0, 4000.0]), 20.0, 70.0, 1013.25) * 1000
    assert 3.5 < a[1] < 6.0  # about 4.7 dB/km at 1 kHz
    assert a[0] < a[1] < a[2]


# ----------------------------------------------------------------------------- rain / wind


def test_rain_curve_and_rendered_level_rise_with_the_rain_rate():
    rates = [0.0, 0.5, 2.0, 10.0, 40.0, 120.0]
    g = ly.rain_gain(rates)
    assert np.all(np.diff(g) > 0)
    levels = []
    for r in (1.0, 5.0, 20.0, 80.0):
        sc = scene(bundle([station(rain=r, dm=1.5)]), dur=6.0)
        out, _ = ly.render_rain(sc, SR, int(6 * SR), 3)
        levels.append(rms(out[:, SR:]))
    assert all(b > a for a, b in itertools.pairwise(levels))


def test_no_rain_means_silence_and_far_rain_is_only_a_quiet_hiss():
    sc = scene(bundle([station(rain=0.0)]), dur=4.0)
    out, _ = ly.render_rain(sc, SR, 4 * SR, 3)
    assert rms(out) < 1e-6
    near = scene(bundle([station(rain=30.0, dm=2.0)]), dur=4.0)
    far = scene(bundle([station(rain=30.0, dm=2.0, de=30000.0)]), dur=4.0)
    n_out, _ = ly.render_rain(near, SR, 4 * SR, 3)
    f_out, _ = ly.render_rain(far, SR, 4 * SR, 3)
    assert rms(f_out) < 0.25 * rms(n_out)
    assert spectral_centroid(f_out[0], SR) > spectral_centroid(n_out[0], SR) * 0.5  # hiss, not a rumble


def test_rain_nearest_station_wins():
    b = bundle([station(rain=2.0, de=500.0, sid="near"), station(rain=100.0, de=40000.0, sid="far")])
    sc = scene(b, dur=3.0)
    v, _d, ok = sc.blend("rain_rate_mmh")
    assert ok.all()
    assert v[100] == pytest.approx(2.0, abs=0.01)


def test_wind_level_and_brightness_rise_with_the_speed():
    assert np.all(np.diff(ly.wind_gain([0.0, 1.0, 3.0, 8.0, 15.0, 30.0])) > 0)
    levels, cents = [], []
    for v in (2.0, 6.0, 12.0, 25.0):
        sc = scene(bundle([station(wind=v)]), dur=6.0)
        out, _ = ly.render_wind(sc, SR, 6 * SR, 5)
        levels.append(rms(out[:, SR:]))
        cents.append(spectral_centroid(out[0, SR:], SR))
    assert all(b > a for a, b in itertools.pairwise(levels))
    assert all(b > a for a, b in itertools.pairwise(cents))


def test_wind_from_the_right_is_louder_on_the_right():
    sc = scene(bundle([station(wind=15.0, wdir=90.0)]), dur=4.0, heading=0.0)  # from the east
    out, _ = ly.render_wind(sc, SR, 4 * SR, 5)
    assert rms(out[1]) > rms(out[0])
    sc = scene(bundle([station(wind=15.0, wdir=270.0)]), dur=4.0, heading=0.0)
    out, _ = ly.render_wind(sc, SR, 4 * SR, 5)
    assert rms(out[0]) > rms(out[1])


# ----------------------------------------------------------------------------- tornado / hail


def test_tornado_roar_rises_with_proximity():
    levels = []
    for d in (9000.0, 3000.0, 1000.0, 400.0):
        sc = scene(bundle(tornadoes=[tor_track(d, 0.0)]), dur=6.0)
        out, info = ly.render_tornado(sc, SR, 6 * SR, 9)
        levels.append(rms(out[:, 2 * SR:]))
        assert info["stylised"] is True
    assert all(b > a for a, b in itertools.pairwise(levels)), levels
    assert ly.tornado_cutoff_hz(8000.0) < ly.tornado_cutoff_hz(500.0)  # muffled when far


def test_tornado_is_silent_without_evidence_or_outside_its_track_time():
    sc = scene(bundle(), dur=6.0)
    out, info = ly.render_tornado(sc, SR, 6 * SR, 9)
    assert np.all(out == 0.0)
    assert info["evidence"] == []
    # a track that exists only before the window
    sc = scene(bundle(tornadoes=[tor_track(500.0, 0.0, t0=-5000.0, t1=-100.0)]), dur=6.0)
    out, _ = ly.render_tornado(sc, SR, 6 * SR, 9)
    assert np.all(out == 0.0)
    # the renderer's event interval restricts the track (data 0..60 s = clip 0..12 s here)
    ev = Event("tornado", None, 2.0, None, 4.0, None, None, 0.0, {})
    sc = scene(bundle(tornadoes=[tor_track(500.0, 0.0)]), dur=8.0, events=[ev])
    out, _ = ly.render_tornado(sc, SR, 8 * SR, 9)
    assert rms(out[:, int(2.5 * SR): int(3.5 * SR)]) > 20 * rms(out[:, int(5.5 * SR):])


def test_hail_only_with_events_near_the_listener():
    sc = scene(bundle([station(rain=60.0, dm=3.0, dbz=50.0)]), dur=8.0)
    out, info = ly.render_hail(sc, SR, 8 * SR, 11)
    assert np.all(out == 0.0)
    assert info["evidence"] == []
    ev = {"type": "hail_candidate", "t_s": 10.0, "t_end_s": 30.0, "dm_mm": 15.0, "dbz": 62.0,
          "rain_rate_mmh": 100.0}  # data 10..30 s = clip 2..6 s at speedup 5
    sc = scene(bundle([station(rain=60.0, dm=3.0, de=500.0, events=[ev])]), dur=8.0)
    out, info = ly.render_hail(sc, SR, 8 * SR, 11)
    assert rms(out[:, 2 * SR: 6 * SR]) > 0
    assert rms(out[:, 7 * SR:]) == 0.0
    assert info["events"] > 0
    # the same event but the station is 30 km away: no hail at the listener
    far = scene(bundle([station(rain=60.0, de=30000.0, events=[ev])]), dur=8.0)
    out, _ = ly.render_hail(far, SR, 8 * SR, 11)
    assert np.all(out == 0.0)


def test_hail_gets_denser_and_louder_with_dm():
    levels = []
    for dm in (6.0, 12.0, 25.0):
        ev = {"type": "hail_candidate", "t_s": 0.0, "t_end_s": 100.0, "dm_mm": dm, "rain_rate_mmh": 80.0}
        sc = scene(bundle([station(rain=60.0, de=300.0, events=[ev])]), dur=6.0)
        out, _ = ly.render_hail(sc, SR, 6 * SR, 11)
        levels.append(rms(out[:, SR:]))
    assert levels[0] < levels[1] < levels[2]


def test_events_json_is_parsed_with_clip_or_iso_times():
    ev = parse_events({"events": [
        {"type": "Tornado", "t_clip_s": 10, "t_end_clip_s": 40, "lat": 33.5, "lon": -88.5},
        {"type": "hail", "time": "2022-03-30T20:01:00Z", "lat": 33.5, "lon": -88.5},
    ]}, WINDOW_START)
    assert ev[0].type == "tornado"
    assert ev[0].t_clip_s == 10
    assert ev[1].t_data_s == pytest.approx(60.0)


def test_convective_bed_follows_the_core_and_is_absent_in_stratiform():
    strat = scene(bundle([station(rain=4.0, dbz=30.0)]), dur=4.0)
    conv = scene(bundle([station(rain=60.0, dbz=55.0, de=200.0)]), dur=4.0)
    s_out, _s_info = ly.render_convective(strat, SR, 4 * SR, 1)
    c_out, c_info = ly.render_convective(conv, SR, 4 * SR, 1)
    assert np.all(s_out == 0.0)
    assert rms(c_out) > 0
    assert c_info["source"] == "station_dbz"


# ----------------------------------------------------------------------------- mix


def test_lufs_meter_reads_a_reference_tone():
    t = np.arange(SR * 3) / SR
    x = np.stack([0.1 * np.sin(2 * np.pi * 1000 * t)] * 2)
    assert mx.lufs(x, SR) == pytest.approx(-20.0, abs=0.3)


def test_priority_ducking_lowers_the_layers_below_a_loud_thunder_only():
    n = 4 * SR
    rng = np.random.default_rng(0)
    bed = 0.01 * rng.standard_normal((2, n))
    th = np.zeros((2, n))
    th[:, 2 * SR: 3 * SR] = 0.3 * rng.standard_normal((2, SR))
    out, reduction, _ = mx.duck_layers({"thunder": th, "wind": bed.copy()}, SR)
    assert reduction["wind"] > 3.0
    assert rms(out["wind"][:, : SR // 2]) == pytest.approx(rms(bed[:, : SR // 2]), rel=0.02)


@pytest.fixture(scope="module")
def storm():
    b, tl, camera = synthetic_scenario()
    y, rep, stems = render_mix(b, tl, camera)
    return b, tl, camera, y, rep, stems


def test_synthetic_two_minute_storm_loudness_peak_and_clipping(storm):
    _, _tl, _, y, rep, _ = storm
    assert y.shape == (2, int(120 * SR))
    m = rep["master"]
    assert m["lufs_integrated"] == pytest.approx(-23.0, abs=0.5)
    assert m["true_peak_dbfs"] < -1.0
    assert m["clipped_samples"] == 0
    assert np.abs(y).max() < 1.0
    assert np.isfinite(y).all()


def test_synthetic_storm_is_mono_compatible_and_report_is_complete(storm):
    _, _, _, _, rep, stems = storm
    assert rep["master"]["mono_compat"]["mono_loss_db"] < 3.0
    assert rep["label"].lower().startswith("synthesised from the data")
    for k in ("thunder", "tornado", "hail", "rain", "wind"):
        assert k in rep["layers"]
        assert "rms_dbfs" in rep["layers"][k]
    assert rep["layers"]["tornado"]["stylised"] is True
    assert rep["layers"]["hail"]["events"] > 0
    assert rep["priority"][:5] == ["thunder", "tornado", "hail", "rain", "wind"]
    fl = rep["thunder"]["flashes"]
    assert len(fl) == 40
    assert sum(rep["thunder"]["counts"].values()) == 40
    row = next(f for f in fl if f["status"] == "rendered")
    for key in ("dist_nearest_m", "dist_farthest_m", "delay_nearest_clip_s", "first_arrival_clip_s", "az_rel_nearest_deg"):
        assert key in row
    json.dumps(rep)  # serialisable
    # the rain peaks with the rain core, the wind with the gust front (data ~300 s = clip ~60 s)
    rl = np.array(rep["layers"]["rain"]["levels_dbfs_per_s"])
    assert rl[50:75].max() > rl[:10].max() + 6.0
    wl = np.array(rep["layers"]["wind"]["levels_dbfs_per_s"])
    assert wl[75:100].mean() > wl[:40].mean() + 3.0
    assert all(np.isfinite(s).all() for s in stems.values())


def test_render_is_deterministic():
    b, _, camera = synthetic_scenario()
    tl = Timeline.constant(25.0, 5.0, data_offset_s=240.0)
    y1, r1, _ = render_mix(b, tl, camera, seed=4)
    y2, r2, _ = render_mix(b, tl, camera, seed=4)
    assert np.array_equal(y1, y2)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
    y3, _, _ = render_mix(b, tl, camera, seed=5)
    assert not np.array_equal(y1, y3)


def test_wav_roundtrip_24_and_16_bit(tmp_path):
    x = (0.3 * np.sin(2 * np.pi * 440 * np.arange(SR) / SR))[:, None] * np.array([1.0, 0.5])
    for bits, tol in ((24, 2e-7), (16, 2e-4)):
        p = tmp_path / f"x{bits}.wav"
        write_wav(p, x, SR, bits)
        y, sr = read_wav(p)
        assert sr == SR
        assert np.abs(y.T - x).max() < tol


# ----------------------------------------------------------------------------- mux

_MUX_SCRIPT = """
import json, sys
import av, numpy as np
sys.path.insert(0, sys.argv[1])
from perils_movie.audio.mux import mux_audio
c = av.open(sys.argv[2], "w", options={"movflags": "+faststart"})
s = c.add_stream("libx264", rate=30); s.width, s.height, s.pix_fmt = 64, 64, "yuv420p"
for i in range(90):
    for p in s.encode(av.VideoFrame.from_ndarray(np.full((64, 64, 3), i, np.uint8), format="rgb24")): c.mux(p)
for p in s.encode(): c.mux(p)
c.close()
print(json.dumps(mux_audio(sys.argv[2], sys.argv[3], sys.argv[4])))
"""


def _av_python():
    try:
        import av  # noqa: F401

        return sys.executable
    except ImportError:
        p = os.environ.get("PERILS_AV_PYTHON")
        return p if p and Path(p).exists() else None


def test_mux_keeps_the_video_frame_count_and_duration(tmp_path):
    py = _av_python()
    if py is None:
        pytest.skip("PyAV not available (set PERILS_AV_PYTHON to a scratch interpreter with av)")
    wav = tmp_path / "a.wav"  # 4 s of audio for a 3 s video: trimmed to the video
    t = np.arange(4 * SR) / SR
    write_wav(wav, np.stack([0.2 * np.sin(2 * np.pi * 330 * t)] * 2, axis=1), SR, 24)
    script = tmp_path / "run.py"
    script.write_text(_MUX_SCRIPT)
    tools = str(Path(__file__).resolve().parents[2] / "tools")
    r = subprocess.run([py, "-I", str(script), tools, str(tmp_path / "in.mp4"), str(wav), str(tmp_path / "out.mp4")],
                       capture_output=True, text=True, check=True)
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["before"]["video_packets"] == res["after"]["video_packets"] == 90
    assert res["after"]["duration_s"] == pytest.approx(res["before"]["duration_s"], abs=0.05)
    assert res["after"]["audio"]["codec"] == "aac"
    assert res["after"]["audio"]["rate"] == SR
    assert res["audio_samples"] == pytest.approx(3.0 * SR, abs=SR * 0.05)

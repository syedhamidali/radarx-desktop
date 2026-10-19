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
from perils_movie.audio import weather as wx
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
    assert sum(v for k, v in counts.items() if k.startswith("skipped")) == 12 - counts["rendered"]
    assert counts.get("skipped_density", 0) > 0


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
        out, _ = wx.render_rain(sc, SR, int(6 * SR), 3)
        levels.append(rms(out[:, SR:]))
    assert all(b > a for a, b in itertools.pairwise(levels))


def test_no_rain_means_silence_and_far_rain_is_only_a_quiet_hiss():
    sc = scene(bundle([station(rain=0.0)]), dur=4.0)
    out, _ = wx.render_rain(sc, SR, 4 * SR, 3)
    assert rms(out) < 1e-6
    near = scene(bundle([station(rain=30.0, dm=2.0)]), dur=4.0)
    far = scene(bundle([station(rain=30.0, dm=2.0, de=30000.0)]), dur=4.0)
    n_out, _ = wx.render_rain(near, SR, 4 * SR, 3)
    f_out, _ = wx.render_rain(far, SR, 4 * SR, 3)
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
        out, _ = wx.render_wind(sc, SR, 6 * SR, 5)
        levels.append(rms(out[:, SR:]))
        cents.append(spectral_centroid(out[0, SR:], SR))
    assert all(b > a for a, b in itertools.pairwise(levels))
    assert all(b > a for a, b in itertools.pairwise(cents))


def test_wind_from_the_right_is_louder_on_the_right():
    sc = scene(bundle([station(wind=15.0, wdir=90.0)]), dur=4.0, heading=0.0)  # from the east
    out, _ = wx.render_wind(sc, SR, 4 * SR, 5)
    assert rms(out[1]) > rms(out[0])
    sc = scene(bundle([station(wind=15.0, wdir=270.0)]), dur=4.0, heading=0.0)
    out, _ = wx.render_wind(sc, SR, 4 * SR, 5)
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
    assert rms(out[:, int(2.5 * SR): int(3.5 * SR)]) > 20 * rms(out[:, int(7.6 * SR):])


def test_hail_only_with_events_near_the_listener():
    sc = scene(bundle([station(rain=60.0, dm=3.0, dbz=50.0)]), dur=8.0)
    out, info = wx.render_hail(sc, SR, 8 * SR, 11)
    assert np.all(out == 0.0)
    assert info["evidence"] == []
    ev = {"type": "hail_candidate", "t_s": 10.0, "t_end_s": 30.0, "dm_mm": 15.0, "dbz": 62.0,
          "rain_rate_mmh": 100.0}  # data 10..30 s = clip 2..6 s at speedup 5
    sc = scene(bundle([station(rain=60.0, dm=3.0, de=500.0, events=[ev])]), dur=8.0)
    out, info = wx.render_hail(sc, SR, 8 * SR, 11)
    assert rms(out[:, 2 * SR: 6 * SR]) > 0
    assert rms(out[:, 7 * SR:]) == 0.0
    assert info["events"] > 0
    # the same event but the station is 30 km away: no hail at the listener
    far = scene(bundle([station(rain=60.0, de=30000.0, events=[ev])]), dur=8.0)
    out, _ = wx.render_hail(far, SR, 8 * SR, 11)
    assert np.all(out == 0.0)


def _hail_scene(**ev_kw):
    ev = {"type": "hail_candidate", "t_s": 0.0, "t_end_s": 100.0, "dm_mm": 8.0, "rain_rate_mmh": 80.0, **ev_kw}
    return scene(bundle([station(rain=60.0, de=300.0, events=[ev])]), dur=6.0)


def test_hail_rate_rises_monotonically_with_the_observed_counts():
    from perils_movie.audio.dsd import SENSOR_AREA_M2

    assert np.all(np.diff(wx.hail_rate_per_s([0.0, 0.1, 1.0, 10.0, 100.0])) > 0)
    rates = []
    for counts in (2, 20, 200, 2000):
        sc = _hail_scene(dmax_mm=15.0, counts=counts)
        _out, info = wx.render_hail(sc, SR, 6 * SR, 11)
        rates.append(info["events"])
        row = info["rows"][0]
        assert row["counts"] == counts
        assert row["large_particle_flux_m2s"] == pytest.approx(counts / (SENSOR_AREA_M2 * 100.0))
        assert row["max_diameter_mm"] == 15.0
        assert row["station_id"] == "S"
    assert rates == sorted(rates)
    assert rates[0] < rates[-1]


def test_hail_gets_louder_and_lower_pitched_with_the_diameter():
    levels, pitch = [], []
    for dmax in (7.0, 14.0, 30.0):
        sc = _hail_scene(dmax_mm=dmax, counts=300)
        out, _ = wx.render_hail(sc, SR, 6 * SR, 11)
        levels.append(rms(out[:, SR:]))
        pitch.append(float(wx.hail_ring_hz(dmax)))
    assert levels[0] < levels[1] < levels[2]
    assert pitch[0] > pitch[1] > pitch[2]


def test_hail_is_deterministic_and_marks_assumed_inputs():
    a, ia = wx.render_hail(_hail_scene(), SR, 6 * SR, 11)
    b, _ = wx.render_hail(_hail_scene(), SR, 6 * SR, 11)
    assert np.array_equal(a, b)
    assert "max_diameter_mm" in ia["rows"][0]["assumed"]
    assert "large_particle_flux" in ia["rows"][0]["assumed"]


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


# ----------------------------------------------------------------------------- drop spectra, MRR, wind events, provenance


def exp_dsd(dm, rate=20.0, nt=3):
    """An exponential drop-size distribution (m^-3 mm^-1) with median volume diameter dm, scaled to
    the given rain rate."""
    from perils_movie.audio.dsd import Dsd, fall_speed_ms

    d = np.linspace(0.25, 9.0, 36)
    dd = np.full_like(d, d[1] - d[0])
    n = np.exp(-3.67 / dm * d)[None, :].repeat(nt, axis=0)
    dsd = Dsd(np.linspace(0.0, 100000.0, nt), d, dd, n, fall_speed_ms(d))
    n = n * rate / dsd.rain_rate_mmh(n)[:, None]
    return Dsd(dsd.t, d, dd, n, dsd.vt)


def dsd_station(dm, rate=20.0, **kw):
    s = station(rain=rate, **kw)
    s.dsd = exp_dsd(dm, rate)
    return s


def tseries(t, v):
    return (np.asarray(t, float), np.asarray(v, float))


def test_dsd_loader_reads_counts_and_concentrations(tmp_path):
    from perils_movie.audio.dsd import dm_from_fall_speed, fall_speed_ms, load_dsd

    d = np.linspace(0.3, 6.0, 20)
    np.savez(tmp_path / "S_dsd.npz", t_s=np.arange(0.0, 300.0, 60.0), diameter_mm=d,
             counts=np.full((5, 20), 50.0))
    dsd = load_dsd(tmp_path / "S_dsd.npz")
    assert dsd is not None
    assert dsd.from_counts is True
    assert dsd.N.shape == (5, 20)
    assert dsd.rain_rate_mmh(dsd.N)[0] > 0
    np.savez(tmp_path / "T_dsd.npz", time_s=np.arange(0.0, 300.0, 60.0), d_mm=d, nd=np.ones((5, 20)))
    assert load_dsd(tmp_path / "T_dsd.npz").from_counts is False
    assert load_dsd(tmp_path / "missing.npz") is None
    # the fall-speed inverse
    assert float(dm_from_fall_speed(fall_speed_ms(2.0))) == pytest.approx(2.0, abs=0.05)


def test_rain_from_the_drop_spectrum_bigger_drops_are_fatter_lower_and_louder():
    out = {}
    for dm in (0.9, 2.8):
        sc = scene(bundle([dsd_station(dm)]), dur=6.0)
        plan = wx.rain_plan(sc)
        assert plan["dsd_used"].all()
        o, info = wx.render_rain(sc, SR, 6 * SR, 3)
        out[dm] = (o[:, SR:], plan, info)
    assert spectral_centroid(out[2.8][0][0], SR) < spectral_centroid(out[0.9][0][0], SR)
    assert rms(out[2.8][0]) > rms(out[0.9][0])
    big = out[2.8][1]["shares"][4:].sum(axis=0).mean()
    small = out[0.9][1]["shares"][4:].sum(axis=0).mean()
    assert big > small  # more of the flux is in the large-drop classes
    assert out[2.8][2]["drop_spectrum_fraction"] == 1.0
    assert out[2.8][2]["drop_spectrum_drivers"][0]["station_id"] == "S"


def test_rain_fallback_with_dm_only_bigger_is_lower_and_louder():
    res = {}
    for dm in (1.0, 3.0):
        sc = scene(bundle([station(rain=20.0, dm=dm)]), dur=6.0)
        o, info = wx.render_rain(sc, SR, 6 * SR, 3)
        res[dm] = o[:, SR:]
        assert info["drop_spectrum_fraction"] == 0.0
    assert spectral_centroid(res[3.0][0], SR) < spectral_centroid(res[1.0][0], SR)
    assert rms(res[3.0]) > rms(res[1.0])


def test_rain_level_still_rises_with_the_rate_when_spectra_are_present():
    levels = []
    for rate in (2.0, 10.0, 50.0):
        sc = scene(bundle([dsd_station(1.8, rate)]), dur=5.0)
        o, _ = wx.render_rain(sc, SR, 5 * SR, 3)
        levels.append(rms(o[:, SR:]))
    assert levels[0] < levels[1] < levels[2]


def test_mrr_lowest_gate_drives_the_drop_size_only_over_the_site():
    def run(distance):
        mrr = station(rain=15.0, sid="MRR", de=distance)
        mrr.type = "mrr"
        mrr.series["mrr_dm_mm"] = tseries([0.0, 1e5], [2.6, 2.6])
        return wx.rain_plan(scene(bundle([mrr]), dur=4.0))

    near = run(400.0)
    far = run(8000.0)
    assert near["mrr_used"].all()
    assert near["dm"].mean() == pytest.approx(2.6, abs=0.01)
    assert (near["dm_src"] == "mrr").all()
    assert not far["mrr_used"].any()
    assert far["dm"].mean() != pytest.approx(2.6, abs=0.2)
    # a fall speed alone is enough: 6.5 m/s is a ~2 mm drop
    s = station(rain=15.0, sid="MRR2")
    s.series["mrr_fall_speed_ms"] = tseries([0.0, 1e5], [6.5, 6.5])
    plan = wx.rain_plan(scene(bundle([s]), dur=3.0))
    assert plan["dm"].mean() == pytest.approx(2.0, abs=0.2)


def _low_energy(x):
    sp = np.abs(np.fft.rfft(x[0])) ** 2
    f = np.fft.rfftfreq(len(x[0]), 1 / SR)
    return float(sp[f < 100].sum())


def test_pressure_rumble_only_with_a_real_pressure_jump():
    t = np.array([0.0, 20.0, 40.0, 60.0, 80.0, 100.0, 200.0])
    flat = station(wind=5.0, sid="A")
    flat.series["pressure_hpa"] = tseries(t, [1000, 1000, 1000.1, 1000.1, 1000, 1000, 1000])
    jump, ev = scene(bundle([flat]), dur=20.0).pressure_jump()
    assert np.all(jump == 0.0)
    assert ev == []
    # two stations with a constant calibration offset are not a jump either
    other = station(wind=5.0, sid="B", de=900.0)
    other.series["pressure_hpa"] = tseries(t, [1010] * 7)
    jump, ev = scene(bundle([flat, other]), dur=20.0).pressure_jump()
    assert np.all(jump == 0.0)
    # a real 2.5 hPa rise around data 40..60 s (clip 8..12 s at speedup 5)
    real = station(wind=5.0, sid="C")
    real.series["pressure_hpa"] = tseries(t, [1000, 1000, 1000.5, 1002.5, 1003, 1003, 1003])
    sc = scene(bundle([real]), dur=20.0)
    jump, ev = sc.pressure_jump()
    assert jump.max() > 0.8
    assert ev[0]["station_id"] == "C"
    assert ev[0]["pressure_rise_hpa"] >= 2.0
    quiet, _ = wx.render_wind(scene(bundle([flat]), dur=20.0), SR, 20 * SR, 5)
    loud, info = wx.render_wind(sc, SR, 20 * SR, 5)
    assert _low_energy(loud) > 3 * _low_energy(quiet)
    assert info["pressure_jumps"]


def test_gust_front_swell_follows_the_observed_wind_jump_and_the_gust_series():
    t = np.array([0.0, 25.0, 35.0, 200.0])
    s = station(sid="G")
    s.series["wind_speed_ms"] = tseries(t, [3.0, 3.0, 20.0, 20.0])  # data 25..35 s = clip 5..7 s
    s.series["wind_dir_deg"] = tseries(t, [180.0] * 4)
    sc = scene(bundle([s]), dur=12.0)
    out, info = wx.render_wind(sc, SR, 12 * SR, 5)
    before = rms(out[:, SR: 4 * SR])
    after = rms(out[:, 8 * SR: 11 * SR])
    assert after > 8 * before  # > 18 dB swell, from the data
    assert info["gust_fronts"]
    assert info["gust_fronts"][0]["wind_rise_ms"] >= 5.0
    # the observed gust column lifts the peaks above the mean
    s2 = station(sid="G2")
    s2.series["wind_speed_ms"] = tseries([0.0, 1e5], [10.0, 10.0])
    s2.series["wind_dir_deg"] = tseries([0.0, 1e5], [180.0, 180.0])
    base = wx.render_wind(scene(bundle([s2]), dur=10.0), SR, 10 * SR, 5)
    s2.series["wind_gust_ms"] = tseries([0.0, 1e5], [22.0, 22.0])
    with_gust = wx.render_wind(scene(bundle([s2]), dur=10.0), SR, 10 * SR, 5)
    assert with_gust[1]["observed_gust_used"] is True
    assert with_gust[1]["effective_speed_ms_max"] > base[1]["effective_speed_ms_max"] + 3.0


def test_report_provenance_names_the_stations_and_instruments_that_drove_each_layer():
    s1 = dsd_station(2.0, 15.0, wind=8.0, sid="PIPS1", de=300.0)
    s1.type, s1.meta = "pips", {"network": "PIPS"}
    s2 = station(rain=5.0, wind=4.0, sid="STK", de=9000.0)
    s2.type = "sticknet"
    s1.events = [{"type": "hail_candidate", "t_s": 0.0, "t_end_s": 60.0, "dmax_mm": 12.0, "counts": 30}]
    tl = Timeline.constant(20.0, 5.0)
    _, rep, _ = render_mix(bundle([s1, s2], tornadoes=[tor_track(2000.0, 0.0)]), tl, cam(0.0, 20.0))
    prov = rep["provenance"]
    assert rep["label"].lower().startswith("synthesised from the data")
    ids = {u["station_id"]: u for u in prov["stations_used"]}
    assert "PIPS1" in ids
    assert {"rain", "wind", "hail"} <= set(ids["PIPS1"]["layers"])
    assert ids["PIPS1"]["type"] == "pips"
    assert prov["rain"][0]["clip_s"][0] == 0.0
    assert prov["hail"][0]["max_diameter_mm"] == 12.0
    assert prov["tornado"]["stylised"] is True
    assert prov["tornado"]["tracks"][0]["id"] == "T"
    for layer in ("thunder", "tornado", "hail", "rain", "wind"):
        assert layer in rep["layers"]
    json.dumps(rep)


# ----------------------------------------------------------------------------- the data agent's bundle layout


def write_real_layout_bundle(d: Path) -> Path:
    """A tiny bundle in the layout written by tools/perils_movie/build_bundle.py (feat/perils-data):
    stations with null values, a PIPS spectrum npz (fall-speed AXIS, counts matrix), hail_events.json,
    an MRR profile npz, a tornado record with a null width, and one radar volume npz."""
    from perils_movie.audio.radar import aeqd_km  # noqa: F401

    (d / "stations").mkdir(parents=True)
    (d / "lma").mkdir()
    (d / "tornadoes").mkdir()
    (d / "profiles").mkdir()
    (d / "volumes").mkdir()
    t = list(np.arange(0.0, 600.0, 10.0))
    nt = len(t)
    pips = {
        "id": "PIPS1B", "name": "PIPS 1B", "type": "PIPS: Parsivel2 disdrometer + surface meteorology",
        "lat": LAT0, "lon": LON0, "alt_m": 90.0, "credit": "Purdue / Penn State", "instrument": "Parsivel2",
        "series": {
            "t_s": t, "rain_rate_mmh": [20.0] * nt, "dm_mm": [None] * 5 + [2.0] * (nt - 5),
            "wind_speed_ms": [6.0] * nt, "wind_gust_ms": [11.0] * nt, "wind_dir_deg": [200.0] * nt,
            "pressure_hpa": [1000.0] * nt, "temperature_c": [20.0] * nt,
        },
    }
    mob = {"id": "Mesonet_DOW7", "name": "DOW7 mesonet", "type": "mobile mesonet (vehicle mounted)",
           "lat": LAT0 + 0.05, "lon": LON0, "alt_m": None, "credit": "UIUC FARM",
           "series": {"t_s": t, "wind_speed_ms": [4.0] * nt, "wind_dir_deg": [180.0] * nt,
                      "lat": [LAT0 + 0.05 - 0.0001 * k for k in range(nt)], "lon": [LON0] * nt}}
    radaps = {"id": "RaDAPS_WXT", "name": "RaDAPS", "type": "mobile profiling site", "lat": LAT0 + 0.01,
              "lon": LON0, "alt_m": 87.0, "credit": "UAH MAPNet",
              "series": {"t_s": t, "rain_rate_mmh": [15.0] * nt, "hail_intensity_hits_cm2h": [0.0] * 30 + [40.0] * 10 + [0.0] * (nt - 40)}}
    (d / "stations" / "stations.json").write_text(json.dumps([pips, mob, radaps]))
    (d / "stations" / "hail_events.json").write_text(json.dumps([
        {"station": "PIPS1B", "t_s": 200.0 + 10 * k, "kind": "hail_candidate", "max_d_mm": 11.0 + k, "n_large": 3}
        for k in range(3)]))
    diam = np.linspace(0.25, 12.0, 24)
    fall = np.linspace(0.0, 20.0, 32)  # the fall-speed AXIS of the matrix: not one value per diameter
    nd = np.exp(-1.8 * diam)[None, :].repeat(nt, axis=0) * 5000.0
    np.savez_compressed(d / "stations" / "PIPS1B_dsd.npz", t_s=np.array(t), diameter_mm=diam,
                        diameter_min_mm=diam - 0.2, diameter_max_mm=diam + 0.2, fallspeed_ms=fall,
                        nd_m3mm=nd.astype(np.float32), counts=np.zeros((nt, 32, 24), np.float32),
                        rain_rate_mmh=np.full(nt, 20.0), dm_mm=np.full(nt, 2.0))
    np.savez_compressed(d / "profiles" / "mrr_RaDAPS.npz", t_s=np.array(t), height_m=np.array([100.0, 200.0, 300.0, 400.0, 900.0]),
                        fall_speed_ms=np.full((nt, 5), -6.5), rain_rate_mmh=np.full((nt, 5), 14.0))
    (d / "tornadoes" / "tornadoes.json").write_text(json.dumps([{
        "id": "NSED_1", "rating": "EF2", "start": "2022-03-30T20:04:00Z", "end": "2022-03-30T20:08:00Z",
        "path": [[240.0, LAT0, LON0 - 0.03], [480.0, LAT0 + 0.02, LON0 + 0.03]], "width_m": None,
        "path_kind": "INTERPOLATED: straight line", "source": "NWS damage survey"}]))
    flash = {"id": 7, "t_start_s": 100.0, "t_end_s": 100.5, "centroid": [LAT0 + 0.05, LON0, 6000.0], "n_sources": 3,
             "sources": [[100.0 + 0.1 * k, LAT0 + 0.05, LON0, 6000.0, None if k == 1 else 8.0] for k in range(3)]}
    (d / "lma" / "flashes.json").write_text(json.dumps({"t0": WINDOW_START, "flashes": [flash]}))
    xs = list(np.arange(-5.0, 5.001, 0.5))
    nz, ny, nx = 8, len(xs), len(xs)
    conv = np.ones((ny, nx), np.uint8)
    conv[8:13, 8:13] = 2
    hid = np.zeros((nz, ny, nx), np.uint8)
    hid[:5, 8:13, 8:13] = 10
    mesh = np.zeros((ny, nx), np.float32)
    mesh[8:13, 8:13] = 32.0
    np.savez_compressed(d / "volumes" / "step_0000.npz", conv_strat=conv, hid=hid, mesh_mm=mesh)
    man = {"window": {"start": WINDOW_START, "end": "2022-03-30T20:10:00Z"},
           "grid": {"origin_lat": LAT0, "origin_lon": LON0, "x_km": xs, "y_km": xs},
           "steps": [{"index": 0, "time": "2022-03-30T20:05:00Z", "volume": "volumes/step_0000.npz"}],
           "sources": [{"name": "PIPS", "credit": "Purdue"}]}
    (d / "manifest.json").write_text(json.dumps(man))
    return d


def test_loads_the_layout_written_by_the_data_agent(tmp_path):
    from perils_movie.audio.bundle import load_bundle

    b = load_bundle(write_real_layout_bundle(tmp_path / "bundle"))
    by = {s.id: s for s in b.stations}
    assert by["Mesonet_DOW7"].alt_m == 0.0  # null altitude
    assert np.isnan(by["PIPS1B"].series["dm_mm"][1][0])  # null value -> NaN
    dsd = by["PIPS1B"].dsd
    assert dsd is not None
    assert dsd.vt.shape == dsd.D.shape  # the fall-speed axis was not taken for one speed per diameter
    assert dsd.dD[0] == pytest.approx(0.4)
    assert dsd.rain_rate_mmh(dsd.N)[0] > 0
    assert len(by["PIPS1B"].events) == 3
    assert by["PIPS1B"].events[0]["counts"] == 3
    assert by["PIPS1B"].events[0]["dmax_mm"] == 11.0
    assert by["PIPS1B"].events[0]["t_end_s"] == 210.0
    assert float(by["RaDAPS_WXT"].series["mrr_fall_speed_ms"][1][0]) == pytest.approx(6.5)
    assert b.tornadoes[0].width_m == 200.0
    assert b.tornadoes[0].meta["path_kind"].startswith("INTERPOLATED")
    assert b.radar_volumes is not None
    s = b.radar_volumes.sample(np.array([300.0]), np.array([LAT0]), np.array([LON0]))
    assert s["ok"][0]
    assert s["conv"][0] == 1.0
    assert s["hail"][0] > 0.5
    assert s["mesh_mm"][0] == 32.0
    far = b.radar_volumes.sample(np.array([300.0]), np.array([LAT0 + 0.5]), np.array([LON0]))
    assert not far["ok"][0]  # outside the grid


def test_end_to_end_on_the_real_layout_with_every_evidence_type(tmp_path):
    b_dir = write_real_layout_bundle(tmp_path / "bundle")
    cam_path = [{"t_clip_s": 0.0, "lat": LAT0, "lon": LON0, "alt_m": 300.0, "heading_deg": 0.0},
                {"t_clip_s": 40.0, "lat": LAT0, "lon": LON0, "alt_m": 300.0, "heading_deg": 0.0}]
    tl = {"data_start": WINDOW_START, "data_end": "2022-03-30T20:10:00Z", "clip_duration_s": 40.0, "fps": 30,
          "speedup": 15.0}
    from perils_movie.audio.render import render_audio

    rep = render_audio(b_dir, tl, cam_path, tmp_path / "out" / "a.wav", seed=2)
    assert (tmp_path / "out" / "audio_report.json").exists()
    lay = rep["layers"]
    assert lay["rain"]["drop_spectrum_fraction"] > 0.9
    assert lay["rain"]["sources"]["dm"]["dsd"] > 0.9
    ev = {r["evidence"] for r in rep["provenance"]["hail"]}
    assert {"station_event", "radar_hail_class", "station_hail_sensor"} <= ev
    merged = next(r for r in rep["provenance"]["hail"] if r["evidence"] == "station_event")
    assert merged["n_samples"] == 3  # three 10 s samples merged into one row
    assert merged["counts"] == 9
    assert merged["max_diameter_mm"] == 13.0
    assert lay["convective"]["source"].startswith("radar_volumes")
    assert lay["convective"]["convective_fraction_of_clip"] == pytest.approx(0.3, abs=0.05)  # one volume, +-90 s of data = 12 s of clip
    assert lay["tornado"]["evidence"] == ["track:NSED_1"]
    assert rep["provenance"]["tornado"]["tracks"][0]["path_kind"].startswith("INTERPOLATED")
    assert rep["master"]["lufs_integrated"] == pytest.approx(-23.0, abs=0.5)
    assert rep["master"]["true_peak_dbfs"] < -1.0
    creds = {u["station_id"]: u for u in rep["provenance"]["stations_used"]}
    assert creds["PIPS1B"]["credit"] == "Purdue / Penn State"
    assert rep["thunder"]["counts"]["rendered"] == 1


def test_a_moving_station_is_followed_by_its_position_series():
    t = np.arange(0.0, 600.0, 10.0)
    mob = station(wind=5.0, sid="M", dn=2000.0)
    la, lo = offset_ll(LAT0, LON0, 0.0, 2000.0 - 3.0 * t)  # drives south at 3 m/s of data time
    mob.series["lat"] = tseries(t, la)
    mob.series["lon"] = tseries(t, np.full_like(t, lo))
    sc = scene(bundle([mob]), dur=20.0, speedup=30.0)
    assert sc.dist[0][0] == pytest.approx(2000.0, abs=30.0)
    assert sc.dist[0][-1] < 400.0


# ----------------------------------------------------------------------------- the renderer's files


def test_timeline_reads_the_renderers_mapping_and_per_frame_speedup():
    t = np.linspace(0.0, 120.0, 3601)
    # speed 8x, slowed to 3x around clip 60 s (what the tornado shot does)
    sp = 8.0 - 5.0 * np.exp(-0.5 * ((t - 60.0) / 8.0) ** 2)
    d = np.concatenate([[0.0], np.cumsum(0.5 * (sp[1:] + sp[:-1]) * np.diff(t))])
    tl = Timeline.from_dict({
        "data_start": "2022-03-30T20:01:00Z", "data_end": "2022-03-30T20:17:00Z", "clip_duration_s": 120.0, "fps": 30,
        "speedup": [float(v) for v in sp[:-1]],
        "mapping": {"t_clip_s": [float(v) for v in t], "t_data_s": [float(v) for v in d]}}, WINDOW_START)
    assert tl.data_offset_s == 60.0  # data_start is one minute after the window start
    assert float(tl.clip_to_data(0.0)) == pytest.approx(60.0)
    assert float(tl.clip_to_data(120.0)) == pytest.approx(60.0 + d[-1])
    for tc in (10.0, 60.0, 100.0):
        assert float(tl.data_to_clip(tl.clip_to_data(tc))) == pytest.approx(tc, abs=1e-6)
    assert float(tl.speedup_at(60.0)) == pytest.approx(3.0, abs=0.1)
    assert float(tl.speedup_at(5.0)) > 7.5
    # without the mapping, the per-frame list of floats gives the same clock
    tl2 = Timeline.from_dict({"clip_duration_s": 120.0, "fps": 30, "speedup": [float(v) for v in sp[:-1]]})
    assert float(tl2.clip_to_data(120.0)) == pytest.approx(d[-1], rel=2e-3)
    # the thunder delay follows the speedup at the time: slower clock -> longer delay in the clip
    f_fast = flash_at(0.0, 6860.0, alt=0.0, t=float(tl.clip_to_data(10.0)))
    f_slow = flash_at(0.0, 6860.0, alt=0.0, t=float(tl.clip_to_data(60.0)))
    r_fast = plan_flash(f_fast, tl, cam(0.0, 120.0), 120.0, ThunderParams())
    r_slow = plan_flash(f_slow, tl, cam(0.0, 120.0), 120.0, ThunderParams())
    assert r_slow["delay_nearest_clip_s"] > 2.0 * r_fast["delay_nearest_clip_s"]


def test_renderers_events_json_dict_is_parsed_and_gates_the_tornado_without_a_track():
    raw = {
        "flashes": [{"id": 1, "t_clip_s": 3.0, "lat": 33.5, "lon": -88.5}],
        "tornado": {"id": "NSED_1", "rating": "EF2", "t_clip_start_s": 2.0, "t_clip_end_s": 6.0,
                    "positions": [{"t_clip_s": 2.0, "lat": LAT0, "lon": LON0 + 0.01},
                                  {"t_clip_s": 6.0, "lat": LAT0, "lon": LON0 + 0.02}]},
        "hail": [{"station": "S", "t_clip_s": 4.0, "t_data_s": 20.0, "lat": LAT0, "lon": LON0, "max_d_mm": 12.0}],
        "nearest_station": [], "shots": [],
    }
    ev = parse_events(raw, WINDOW_START)
    assert [e.type for e in ev] == ["tornado", "hail", "flash"]
    sc = scene(bundle([station(rain=5.0)]), dur=14.0, events=ev)
    dist, exist, srcs = sc.tornado()
    assert srcs == ["event"]
    assert exist[int(4.0 * 100)] > 0.7  # a 4 s event, smoothed by the 4 s ramp
    assert exist[int(11.0 * 100)] == 0.0
    assert dist[int(4.0 * 100)] == pytest.approx(1500.0, rel=0.1)  # ~0.015 deg of longitude
    out, _ = wx.render_hail(sc, SR, 8 * SR, 1)
    assert np.all(out == 0.0)  # the repeated station hail event is counted only through the station


def test_thunder_coverage_and_voice_limits_keep_the_mix_readable():
    prm = ThunderParams(max_voices=2, max_coverage=0.3)
    tl = Timeline.constant(60.0, 5.0)
    rows = [plan_flash(flash_at(0.0, 4000.0 + 3000.0 * i, alt=0.0, t=50.0 + 10.0 * i, fid=f"f{i}"), tl,
                       cam(0.0, 60.0), 60.0, prm) for i in range(30)]
    counts = select_flashes(rows, prm, 60.0)
    assert counts["rendered"] >= 1
    assert counts.get("skipped_coverage", 0) + counts.get("skipped_voices", 0) > 0
    assert sum(counts.values()) == 30
    occupied = np.zeros(601, bool)
    for r in rows:
        if r["status"] == "rendered":
            occupied[int(r["first_arrival_clip_s"] * 10): int(r["last_arrival_clip_s"] * 10) + 1] = True
    window = np.convolve(occupied.astype(float), np.ones(100) / 100, mode="valid")
    assert window.max() <= 0.3 + 1e-9  # in any 10 s of the clip


# ----------------------------------------------------------------------------- radar rain, listener height


def _radar_bundle(tmp_path, dbz=45.0):
    from perils_movie.audio.bundle import load_bundle

    d = write_real_layout_bundle(tmp_path / "bundle")
    # the volume gets a reflectivity field: dbz over the 5x5 core around the centre, NaN elsewhere
    z = np.load(d / "volumes" / "step_0000.npz")
    arrs = {k: z[k] for k in z.files}
    ref = np.full(arrs["hid"].shape, np.nan, np.float32)
    ref[:3, 6:15, 6:15] = dbz
    np.savez_compressed(d / "volumes" / "step_0000.npz", dbz=ref, **arrs)
    return load_bundle(d)


def test_radar_reflectivity_gives_the_rain_where_no_instrument_is_near(tmp_path):
    b = _radar_bundle(tmp_path)
    s = b.radar_volumes.sample(np.array([300.0]), np.array([LAT0]), np.array([LON0]))
    assert s["dbz_low"][0] == pytest.approx(45.0, abs=0.1)
    assert s["rain_mmh"][0] == pytest.approx((10 ** 4.5 / 300.0) ** (1 / 1.4), rel=0.02)
    from perils_movie.audio.radar import zr_rain_rate

    assert float(zr_rain_rate(10 ** 0.5)) == 0.0  # below 12 dBZ: no rain
    assert float(zr_rain_rate(1e9)) == 150.0  # capped
    # stations only far away (> 8 km): the radar over the listener decides
    for s_ in b.stations:
        s_.lat, s_.lon = LAT0 + 0.3, LON0  # 33 km north
    b.stations = [s for s in b.stations if "rain_rate_mmh" in s.series][:1]
    sc = scene(b, dur=10.0, speedup=30.0)  # clip 0..10 s = data 0..300 s
    plan = wx.rain_plan(sc)
    mid = int(10.0 * 100 - 1)  # data ~300 s, the volume time
    assert plan["radar_used"][mid]
    assert plan["rate_src"][mid] == "radar_zr"
    assert plan["R"][mid] == pytest.approx((10 ** 4.5 / 300.0) ** (1 / 1.4), rel=0.05)
    assert plan["wd"][mid] == 1.0
    # an instrument near the listener (< 8 km) is the rain: the radar is not used
    b.stations[0].lat, b.stations[0].lon = LAT0 + 0.02, LON0
    sc2 = scene(b, dur=10.0, speedup=30.0)
    plan2 = wx.rain_plan(sc2)
    assert not plan2["radar_used"][mid]
    assert plan2["rate_src"][mid] == "station"
    # no echo over the listener: far rain is not heard (the radar says there is none)
    b0 = _radar_bundle(tmp_path / "dry", dbz=5.0)
    b0.stations = b.stations[:1]
    b0.stations[0].lat, b0.stations[0].lon = LAT0 + 0.3, LON0
    _out, info = wx.render_rain(scene(b0, dur=6.0, speedup=50.0), SR, 6 * SR, 3)
    assert info["sources"]["rain_rate"]["radar_zr"] > 0.2  # the volume is valid +-90 s of data around its time
    assert info["radar_drivers"] == []


def test_listener_is_the_camera_ground_track_capped_in_height():
    b, tl, _camera = synthetic_scenario()
    high = CameraPath([{"t_clip_s": 0.0, "lat": LAT0, "lon": LON0, "alt_m": 58000.0, "heading_deg": 0.0},
                       {"t_clip_s": 120.0, "lat": LAT0, "lon": LON0, "alt_m": 58000.0, "heading_deg": 0.0}])
    _, rep, _ = render_mix(b, tl, high, layers={"rain", "wind"})
    assert rep["params"]["listener"]["camera_altitude_max_m"] == 58000.0
    assert rep["params"]["listener"]["max_height_above_ground_m"] == 500.0
    assert high.capped(600.0).alt.max() == 600.0
    assert high.alt.max() == 58000.0  # the original path is untouched
    # without the cap a 58 km high listener hears nothing of the stations on the ground
    _, rep_nocap, _ = render_mix(b, tl, high, layers={"rain", "wind"}, max_listener_height_m=None)
    assert rep["layers"]["rain"]["coverage"] > 0.5
    assert rep_nocap["layers"]["rain"]["coverage"] < 0.01

"""Tests of the tornado focus of the PERiLS movie: the data clock built from segments, the six camera angles placed from the
surveyed tornado, the consistency of the camera's position with its latitude and longitude, the low-level radar products
(storm-relative radial velocity, azimuthal shear, the rotation series) and their conversion. No browser."""

from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "perils_movie" / "render"))

choreography = importlib.import_module("choreography")
convert_bundle = importlib.import_module("convert_bundle")
convert_focus = importlib.import_module("convert_focus")
synth_bundle = importlib.import_module("synth_bundle")
focus = importlib.import_module("focus")
focus_plan = importlib.import_module("focus_plan")


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    root = tmp_path_factory.mktemp("perils_focus")
    synth_bundle.write(root / "bundle", 5, 120.0, res_km=1.5, dz_km=1.0)
    perils = convert_bundle.convert(root / "bundle", root / "clip", log=lambda *_: None)
    return root, perils


# ── the data clock ──────────────────────────────────────────────────────────

SEGS = [
    focus.Seg("a", 6.0, "cruise", 0.0),
    focus.Seg("b", 10.0, "rt", 100.0),
    focus.Seg("c", 3.0, "lapse", 110.0),
    focus.Seg("d", 8.0, "rt", 200.0),
    focus.Seg("e", 5.0, "cruise", 208.0),
]


def test_the_segment_clock_hits_every_anchor_and_runs_in_real_time_where_asked():
    tl = focus.build_timeline_segments(SEGS, 400.0, 30)
    t, td, sp = tl["t_clip_s"], tl["t_data_s"], tl["speedup"]
    assert (
        tl["frames"] == 32 * 30
        and td[0] == pytest.approx(0.0)
        and td[-1] == pytest.approx(400.0, abs=0.02)
    )
    assert np.all(np.diff(td) > 0)
    assert float(np.interp(6.0, t, td)) == pytest.approx(
        100.0, abs=0.05
    )  # the first real-time stretch starts where it should
    assert float(np.interp(19.0, t, td)) == pytest.approx(
        200.0, abs=0.05
    )  # and the second: a lapse covers 90 data seconds in 3
    # inside a real-time stretch the clock is exactly one data second per clip second, all through it
    for a, b in ((6.0, 16.0), (19.0, 27.0)):
        m = (t >= a + 0.1) & (t <= b - 0.1)
        assert np.abs(sp[m] - 1.0).max() < 1e-6
    # no jump in the speed-up anywhere (a speed that is continuous), and the lapse is the fast part
    assert (
        np.abs(np.diff(sp)).max() < 3.0
    )  # the 3 s lapse peaks near x45: a change of about 2 per frame at most
    assert sp[int(17.5 * 30)] > 20
    assert float(np.interp(16.0, t, td)) == pytest.approx(110.0, abs=0.05)


def test_segments_that_do_not_fit_are_refused_or_become_a_cruise():
    with pytest.raises(ValueError):
        focus.build_timeline_segments(
            [focus.Seg("a", 10.0, "rt", 0.0), focus.Seg("b", 5.0, "cruise", 5.0)], 100.0
        )
    segs = focus_plan.make_segments(
        focus_plan.FILM_SEGS, 120.0, 48.0
    )  # a short case: the touchdown at 48 s of a 120 s window
    assert all(s.kind in ("cruise", "rt", "lapse") for s in segs)
    tl = focus.build_timeline_segments(segs, 120.0, 30)
    assert np.all(np.diff(tl["t_data_s"]) > 0) and tl["t_data_s"][-1] == pytest.approx(
        120.0, abs=0.05
    )
    # for the real case (touchdown at 240 s of 960) the anchors are kept as they are
    real = focus_plan.make_segments(focus_plan.FILM_SEGS, 960.0, 240.0)
    assert [round(s.td0, 6) for s in real] == [td for _n, _l, _k, td in focus_plan.FILM_SEGS]
    assert [s.kind for s in real] == [k for _n, _l, k, _td in focus_plan.FILM_SEGS]


# ── the plan ────────────────────────────────────────────────────────────────


def test_the_film_and_the_clip_have_their_lengths_and_the_same_six_angles(tiny):
    _root, p = tiny
    film = focus_plan.plan_focus(p, "film")
    clip = focus_plan.plan_focus(p, "clip")
    assert film["timeline"]["clip_duration_s"] == pytest.approx(120.0)
    assert 60.0 <= clip["timeline"]["clip_duration_s"] <= 90.0
    names = ["focus_wide", "focus_chaser", "focus_close", "focus_low", "focus_swath", "focus_top"]
    for res in (film, clip):
        got = [s.name for s in res["shots"] if s.name.startswith("focus_")]
        assert got == names
        assert (
            res["timeline"]["focus"]["beats"]
            and res["timeline"]["focus"]["sim_span_data_s"][0]
            < res["timeline"]["focus"]["sim_span_data_s"][1]
        )
    # the film keeps the flyby and the finale; the clip is the focus alone
    assert [s.name for s in film["shots"]][:2] == ["establishing", "flyby"] and film["shots"][
        -1
    ].name == "topdown"
    assert clip["shots"][0].name == "focus_wide"
    assert film["timeline"]["end_card_s"] > clip["timeline"]["end_card_s"]


def test_the_camera_has_no_jumps_and_its_latitude_and_longitude_are_its_own_position(tiny):
    _root, p = tiny
    scene = choreography.Scene(p)
    for layout in ("film", "clip"):
        cam = focus_plan.plan_focus(p, layout)["camera"]
        x = np.array([c["x_km"] for c in cam])
        y = np.array([c["y_km"] for c in cam])
        h = np.array([c["heading_deg"] for c in cam])
        dh = np.abs((np.diff(h) + 180) % 360 - 180)
        assert dh.max() < 3.0  # degrees per frame: no whip
        assert (
            np.hypot(np.diff(x), np.diff(y)).max() < 6.0
        )  # km per frame (the glide to the ground-level station is the fastest)
        # the lat / lon the sound and the simulation read are the position of x_km, y_km in the bundle's projection (a metre)
        for c in cam[::37]:
            la, lo = scene.geo.latlon(c["x_km"], c["y_km"])
            assert abs(c["lat"] - float(la)) < 2e-5 and abs(c["lon"] - float(lo)) < 2e-5
        assert all(c["fov_deg"] > 20 for c in cam) and all(
            0 <= c["pitch_deg"] + 90 <= 180 for c in cam
        )


def test_the_angles_stand_where_they_are_said_to_stand_relative_to_the_surveyed_tornado(tiny):
    _root, p = tiny
    res = focus_plan.plan_focus(p, "film")
    scene = choreography.Scene(p)
    rig = focus.Rig(scene, res["tl"])
    beats = {b["name"]: b for b in res["events"]["focus"]["beats"]}
    cam = res["camera"]
    right = rig.right_deg

    def at(name):
        b = beats[name]
        c = cam[round(0.5 * (b["t0"] + b["t1"]) * 30)]
        q = rig.T(0.5 * (b["t0"] + b["t1"]))[0]
        d = math.hypot(c["x_km"] - q[0], c["y_km"] - q[1])
        brg = float(choreography.bearing_deg(c["x_km"] - q[0], c["y_km"] - q[1]))
        return c, d, brg

    c, d, brg = at("focus_chaser")
    assert (
        d == pytest.approx(focus.CHASER_DIST_KM, rel=0.15)
        and c["alt_m"] < 40
        and c["fov_deg"] == pytest.approx(focus.CHASER_FOV, abs=0.5)
    )
    assert (
        abs(((brg - (right + focus.CHASER_BEARING_OFF)) + 180) % 360 - 180) < 12
    )  # on the inflow side, to the right of the track
    c, d, brg = at("focus_close")
    assert (
        d == pytest.approx(focus.CLOSE_DIST_KM, rel=0.15)
        and c["alt_m"] < 10
        and abs(((brg - right) + 180) % 360 - 180) < 12
    )
    c, d, _ = at("focus_low")
    assert d < 0.45 and c["alt_m"] < 10 and c["pitch_deg"] > 15  # near, low and looking up
    c, d, _ = at("focus_swath")
    assert (
        0.3 < d < 0.8 and 40 < c["alt_m"] < 150 and c["pitch_deg"] < 0
    )  # behind the tornado, looking down at the path
    c, d, _ = at("focus_wide")
    assert d > 3.0 and c["alt_m"] > 1000  # far and high, outside the strong echo
    # the camera of the real-time angles looks at the tornado (it is in the frame, near the axis)
    for name in ("focus_chaser", "focus_close", "focus_low"):
        b = beats[name]
        for tc in (b["t0"] + 3.0, 0.5 * (b["t0"] + b["t1"]), b["t1"] - 1.0):
            c = cam[round(tc * 30)]
            q = rig.T(tc)[0]
            off = (
                (
                    float(choreography.bearing_deg(q[0] - c["x_km"], q[1] - c["y_km"]))
                    - c["heading_deg"]
                )
                + 180
            ) % 360 - 180
            assert abs(off) < 25.0


def test_the_real_time_angles_run_at_one_data_second_per_clip_second_and_say_so(tiny):
    _root, p = tiny
    res = focus_plan.plan_focus(p, "film")
    for b in res["events"]["focus"]["beats"]:
        if b["kind"] == "rt":
            assert b["speedup_mean"] == pytest.approx(1.0, abs=0.02)
    # the survey position through the clock: the events' tornado positions are the survey line at the data times
    path = next(t["path"] for t in p["tornadoes"] if len(t.get("path", [])) >= 2)
    t0, t1 = path[0]["t"], path[-1]["t"]
    geo = choreography.Scene(p).geo
    for q in res["events"]["tornado"]["positions"][::5]:
        td = float(choreography.data_time_of_clip(res["tl"], q["t_clip_s"]))
        f = (td - t0) / (t1 - t0)
        la, lo = geo.latlon(
            path[0]["x"] + (path[-1]["x"] - path[0]["x"]) * f,
            path[0]["y"] + (path[-1]["y"] - path[0]["y"]) * f,
        )
        assert abs(q["lat"] - float(la)) < 2e-5 and abs(q["lon"] - float(lo)) < 2e-5
    assert (
        res["events"]["tornado"]["shot"]["t1_clip_s"]
        > res["events"]["tornado"]["shot"]["t0_clip_s"]
    )


def test_shot_lenses_and_shake_are_per_shot_and_blend(tiny):
    _root, p = tiny
    res = focus_plan.plan_focus(p, "film")
    cam = res["camera"]
    by = {}
    for c in cam:
        by.setdefault(c["shot"], []).append(c["fov_deg"])
    assert max(by["focus_low"]) == pytest.approx(focus.LOW_FOV, abs=0.5) and min(
        by["focus_chaser"]
    ) == pytest.approx(focus.CHASER_FOV, abs=0.5)
    assert cam[0]["fov_deg"] == pytest.approx(45.0)
    fov = np.array([c["fov_deg"] for c in cam])
    assert np.abs(np.diff(fov)).max() < 0.6  # the lens changes smoothly


# ── the radar products ──────────────────────────────────────────────────────


def test_signed_codes_round_trip_and_keep_no_data():
    v = np.array([-35.0, -10.5, 0.0, 12.3, 35.0, np.nan, 50.0])
    c = convert_focus.encode_signed(v, 35.0)
    assert c[5] == 0 and c[0] == 1 and c[4] == 255 and c[6] == 255
    d = convert_focus.decode_signed(c, 35.0)
    assert np.allclose(d[[0, 1, 2, 3, 4]], v[[0, 1, 2, 3, 4]], atol=0.28)
    assert np.isnan(d[5])
    a = convert_focus.encode_unsigned(np.array([-0.01, 0.0, 0.02, 0.04, 0.08, np.nan]), 0.04)
    assert list(a[:2]) == [0, 0] and a[3] == 255 and a[4] == 255 and a[5] == 0 and 120 < a[2] < 136


def test_storm_relative_radial_velocity_removes_the_motion_along_each_beam():
    xs = np.arange(-20.0, 20.5, 0.5)
    ys = np.arange(-20.0, 20.5, 0.5)
    xx, yy = np.meshgrid(xs, ys)
    radar = (-30.0, 5.0)
    wind = np.array([10.0, 5.0])
    dx, dy = xx - radar[0], yy - radar[1]
    r = np.hypot(dx, dy)
    vel = (wind[0] * dx + wind[1] * dy) / r  # what a radar at ``radar`` measures of a uniform wind
    src = np.ones(vel.shape, np.uint8)
    out = convert_focus.storm_relative_radial(vel, src, xs, ys, [radar], (wind[0], wind[1]))
    assert np.nanmax(np.abs(out)) < 1e-9  # moving with the wind: nothing left
    out0 = convert_focus.storm_relative_radial(vel, src, xs, ys, [radar], (0.0, 0.0))
    assert np.allclose(out0, vel)
    out2 = convert_focus.storm_relative_radial(vel, src, xs, ys, [radar], (10.0, 0.0))
    assert np.nanmax(np.abs(out2 - (5.0 * dy / r))) < 1e-9
    # a voxel without a source radar, and the 1.5 km around a radar, have no value
    src[0, 0] = 0
    assert np.isnan(
        convert_focus.storm_relative_radial(vel, src, xs, ys, [radar], (0.0, 0.0))[0, 0]
    )


def test_lowest_valid_pair_takes_the_first_level_with_a_velocity():
    vel = np.full((6, 3, 3), np.nan)
    src = np.zeros((6, 3, 3), np.uint8)
    vel[2, 0, 0], src[2, 0, 0] = 4.0, 3
    vel[3, 0, 0], src[3, 0, 0] = 9.0, 5
    vel[3, 1, 1], src[3, 1, 1] = -2.0, 2
    v, s = convert_focus.lowest_valid_pair(vel, src)
    assert (
        v[0, 0] == 4.0
        and s[0, 0] == 3
        and v[1, 1] == -2.0
        and s[1, 1] == 2
        and np.isnan(v[2, 2])
        and s[2, 2] == 0
    )


def test_the_rotation_sample_finds_the_maximum_near_the_survey_position_and_says_where():
    xs = np.arange(0.0, 20.0, 0.5)
    ys = np.arange(0.0, 20.0, 0.5)
    rot = np.zeros((len(ys), len(xs)))
    vr = np.zeros_like(rot)
    src = np.full(rot.shape, 6, np.uint8)
    rot[int(12 / 0.5), int(8 / 0.5)] = 0.02  # at x = 8, y = 12
    vr[int(12 / 0.5), int(8 / 0.5)] = 17.0
    rot[int(2 / 0.5), int(18 / 0.5)] = 0.05  # stronger, but far from the survey position
    s = convert_focus.vortex_sample(rot, vr, src, xs, ys, (9.0, 10.5))
    assert s["as"] == pytest.approx(0.02) and s["vrot"] == pytest.approx(17.0)
    assert s["x"] == 8.0 and s["y"] == 12.0
    assert s["offset_km"] == pytest.approx(math.hypot(1.0, 1.5)) and s[
        "bearing_deg"
    ] == pytest.approx(math.degrees(math.atan2(-1.0, 1.5)) % 360)
    assert s["radar_index"] == 6
    none = convert_focus.vortex_sample(np.zeros_like(rot), vr, src, xs, ys, (9.0, 10.5))
    assert none["as"] is None and none["offset_km"] is None


def test_the_echo_context_gives_the_nearest_strong_echo_with_its_bearing():
    xs = np.arange(0.0, 20.0, 0.5)
    ys = np.arange(0.0, 20.0, 0.5)
    c = np.full((len(ys), len(xs)), 30.0)
    c[int(15 / 0.5), int(10 / 0.5)] = 52.0  # 5 km north of (10, 10)
    c[int(10 / 0.5), int(2 / 0.5)] = 55.0  # 8 km west
    e = convert_focus.echo_context(c, xs, ys, (10.0, 10.0))
    assert e["core50_km"] == pytest.approx(5.0) and e["core50_bearing_deg"] == pytest.approx(0.0)
    assert e["core40_km"] == pytest.approx(5.0) and e["cmax_at_dbz"] == pytest.approx(30.0)
    assert (
        convert_focus.echo_context(np.full_like(c, 20.0), xs, ys, (10.0, 10.0))["core50_km"] is None
    )


def test_the_wind_context_is_the_storm_relative_flow_and_where_it_comes_from(tmp_path):
    n = 21
    z = np.arange(0.0, 5.0, 0.25)
    x = np.arange(-10.0, 11.0, 1.0)
    u = np.zeros((len(z), n, n), np.float32)
    v = np.full(
        (len(z), n, n), 19.0, np.float32
    )  # blowing toward the north: it comes from the south
    f = tmp_path / "w.npz"
    np.savez(f, x_km=x, y_km=x, z_km=z, u_sr=u, v_sr=v)
    w = convert_focus.wind_context(f, (0.0, 0.0))
    assert (
        w["speed_ms"] == pytest.approx(19.0)
        and w["from_deg"] == pytest.approx(180.0)
        and w["valid_fraction"] == pytest.approx(1.0)
    )
    assert convert_focus.wind_context(tmp_path / "missing.npz", (0.0, 0.0)) is None


def test_the_conversion_writes_the_focus_images_and_the_series(tiny):
    root, p = tiny
    f = p["focus"]
    assert f is not None and len(f["series"]) == len(p["steps"])
    for m in f["vsr"] + f["shear"]:
        assert (root / "clip" / m["bin"]).stat().st_size == m["nx"] * m["ny"]
    ph = {s["phase"] for s in f["series"]}
    assert "on" in ph and f["shearLim"] > 0 and f["vsrLim"] > 0
    assert all({"t", "ref_x", "ref_y", "phase"} <= set(s) for s in f["series"])
    assert p["echo"]["manifest"] == "manifest.json" and p["echo"]["quality"] is False

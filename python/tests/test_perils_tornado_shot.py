"""Tests of the tornado shot of the PERiLS movie: the real-time core of the timeline, the close and low camera with a
wide lens and a bounded hand-held shake, the immersive grade, the rotation numbers (what the radars measured and what is
estimated), the measurement of the rotation in rendered frames, and the plan-view columns of the conversion. No browser."""

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
synth_bundle = importlib.import_module("synth_bundle")
vortex = importlib.import_module("vortex")
measure_rotation = importlib.import_module("measure_rotation")


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    root = tmp_path_factory.mktemp("perils_tornado")
    synth_bundle.write(root / "bundle", 5, 120.0, res_km=1.5, dz_km=1.0)
    perils = convert_bundle.convert(root / "bundle", root / "clip", log=lambda *_: None)
    return root, perils


SURVEY_ESTIMATE = {
    "wind_range_ms": [49.6, 60.4],
    "damage_width_m": 366.0,
    "rotation_period_s_range": [19.1, 23.2],
}
KINEMATICS = {
    "survey_scale_estimate": SURVEY_ESTIMATE,
    "summary": {
        "vrot_ms_min_median_max": [7.9, 11.2, 15.2],
        "couplet_diameter_km_min_median_max": [1.1, 1.8, 5.9],
        "omega_s1_median_cyclonic": 0.0128,
        "cyclonic_steps": 11,
        "n_steps": 13,
    },
    "steps": [
        {"t": 240.0, "vrot_ms": 7.9, "diameter_km": 2.4, "sense": "cyclonic", "radar": "NOXP"},
        {"t": 600.0, "vrot_ms": 15.2, "diameter_km": 1.1, "sense": "cyclonic", "radar": "NOXP"},
    ],
}


def test_the_timeline_has_a_core_that_runs_in_real_time_and_still_covers_the_window():
    tl = choreography.build_timeline(
        960.0, 120.0, 30, dip_centre=58.0, dip_width=24.0, core_speedup=1.0, core_width=10.0
    )
    sp, td = tl["speedup"], tl["t_data_s"]
    assert td[0] == 0 and td[-1] == pytest.approx(960.0)
    assert np.all(np.diff(td) > 0)
    assert sp[58 * 30] == pytest.approx(1.0, abs=0.02)  # real time in the middle
    assert sp[54 * 30] < 1.3 and sp[62 * 30] < 1.3  # and around it
    assert sp[10 * 30] > 8  # the rest runs faster to make up for it


def test_smooth_keys_pass_near_the_keys_and_do_not_jump():
    keys = [(0.0, 2.6), (4.5, 0.9), (8.0, 0.34), (12.0, 0.34), (20.0, 0.8)]
    t = np.linspace(0, 20, 801)
    v = choreography.smooth_keys(t, keys)
    assert np.all(np.isfinite(v))
    assert abs(v[400] - 0.34) < 0.12 and v.min() > 0.25
    assert np.abs(np.diff(v)).max() < 0.1


def test_the_rotation_numbers_are_the_survey_estimate_and_the_radar_measures_only_the_parent_circulation():
    tor = {"rating": "EF2", "width_m": 366.0}
    p = vortex.vortex_params(tor, KINEMATICS, 400.0)
    assert p["radius_m"] == pytest.approx(183.0)
    assert p["vt_ms"] == pytest.approx(55.0)
    assert p["period_s"] == pytest.approx(2 * math.pi * 183.0 / 55.0)
    assert 19.1 < p["period_s"] < 23.2  # inside the survey's range
    assert p["omega_rad_s"] == pytest.approx(55.0 / 183.0)
    assert p["tornado_resolved"] is False
    assert p["parent_circulation"]["median_vrot_ms"] == 11.2
    assert p["parent_circulation"]["median_diameter_km"] == 1.8
    # without the kinematics file the EF range of the rating gives the same number
    q = vortex.vortex_params(tor, None, 400.0)
    assert q["vt_ms"] == pytest.approx(55.0, abs=0.1) and q["parent_circulation"] is None
    assert vortex.vortex_params({"rating": "unknown"}, None, 0.0) is None


def test_the_tornado_shot_goes_close_and_low_with_a_wide_lens_and_a_bounded_shake(tiny):
    _root, p = tiny
    res = choreography.plan(p)
    cam = res["camera"]
    scene = choreography.Scene(p)
    tl = res["tl"]
    tor = [c for c in cam if c["shot"] == "tornado"]
    assert tor
    dmin, zmin = 1e9, 1e9
    for c in tor:
        q, _a = scene.tornado_at(float(choreography.data_time_of_clip(tl, c["t_clip_s"])))
        dmin = min(dmin, math.hypot(c["x_km"] - q[0], c["y_km"] - q[1]))
        zmin = min(zmin, c["alt_m"])
    assert dmin < 0.4 and zmin < 100.0  # within 400 m of the tornado and under 100 m high
    mid = tor[len(tor) // 3 : -(len(tor) // 3)]
    assert all(c["vexag"] <= 1.2 for c in mid)  # true scale in the middle
    assert max(c["fov_deg"] for c in cam) == pytest.approx(choreography.WIDE_FOV, abs=0.5)
    assert cam[0]["fov_deg"] == pytest.approx(45.0)  # the renderer's default elsewhere
    w = choreography.immersion(res["shots"], np.asarray(tl["t_clip_s"]))
    assert w.max() == pytest.approx(1.0) and w[0] == 0.0
    s = choreography.shake_signal(np.arange(0, 20, 1 / 30), 0.4)
    assert np.abs(s).max() <= 1.6 + 1e-9
    assert np.abs(np.diff(s)).max() * choreography.SHAKE["heading_deg"] < 0.2  # degrees per frame
    assert choreography.SHAKE["heading_deg"] * 1.6 < 0.6 and choreography.SHAKE["pos_m"] < 1.0


def test_the_events_carry_the_vortex_and_the_real_time_window(tiny):
    _root, p = tiny
    q = dict(p)
    q["vortex"] = KINEMATICS
    res = choreography.plan(q)
    vx = res["events"]["tornado"]["vortex"]
    assert vx["radius_m"] == pytest.approx(183.0)
    assert vx["period_s"] == pytest.approx(20.91, abs=0.02)
    assert vx["speedup_at_middle"] == pytest.approx(1.0, abs=0.05)
    assert vx["real_time_to_clip_s"] - vx["real_time_from_clip_s"] > 8.0
    assert vx["tornado_resolved"] is False
    assert vx["parent_circulation"]["median_vrot_ms"] == 11.2
    assert "no radar resolves" in vx["note"]
    tm = res["timeline"]["tornado_slowdown"]
    assert tm["core_speedup"] == 1.0 and tm["core_width_s"] == 10.0


def test_the_aim_is_blended_so_that_the_view_does_not_whip_when_a_shot_arrives(tiny):
    _root, p = tiny
    cam = choreography.plan(p)["camera"]
    h = np.array([c["heading_deg"] for c in cam])
    dh = np.abs((np.diff(h) + 180) % 360 - 180)
    assert dh.max() < 2.0
    tor_i = [i for i, c in enumerate(cam) if c["shot"] == "tornado"]
    assert dh[tor_i[0] : tor_i[-1]].max() < 1.2  # inside the tornado shot, an orbit, not a whip


def _ring_pixels(omega, times, centre=(5.0, -3.0), radius=0.229, fov_deg=64.0):
    """A tracer on a ground ring seen by a camera ~350 m away at 37 m: pixel, back-projection, centre."""
    w, h = 1920, 1080
    tan_y = math.tan(math.radians(fov_deg) / 2)
    tan_x = tan_y * w / h
    out = []
    for t in times:
        az = 0.6 + omega * t
        pt = np.array([centre[0] + radius * math.cos(az), centre[1] + radius * math.sin(az), 0.012])
        eye = (centre[0] + 0.30, centre[1] - 0.18 + 0.05 * math.sin(t * 0.2), 0.037)
        fwd, right, up = measure_rotation.camera_basis(
            math.degrees(math.atan2(centre[0] - eye[0], centre[1] - eye[1])), 20.0
        )
        px, py, _d = measure_rotation.project(pt, eye, fwd, right, up, tan_x, tan_y, w, h)
        back = measure_rotation.backproject(px, py, eye, fwd, right, up, tan_x, tan_y, w, h, 0.012)
        out.append((az, back, centre))
    return out


def test_the_measurement_recovers_the_rotation_rate_from_pixels():
    omega = 0.3005
    times = np.arange(0.0, 12.0, 0.2)
    pts = _ring_pixels(omega, times)
    az = [math.atan2(b[1] - c[1], b[0] - c[0]) for _a, b, c in pts]
    rate, rms = measure_rotation.fit_rate(np.array(az), times)
    assert rate == pytest.approx(omega, rel=1e-3) and rms < 1e-3
    rate2, _ = measure_rotation.fit_rate(-np.array(az), times)
    assert rate2 < 0  # a clockwise tracer gives a negative rate
    r = [math.hypot(b[0] - c[0], b[1] - c[1]) for _a, b, c in pts]
    assert np.median(r) == pytest.approx(0.229, abs=1e-3)


def test_the_tracer_is_found_in_a_frame():
    img = np.zeros((1080, 1920, 3), np.uint8)
    img[500:520, 900:930] = (0, 255, 230)
    img[10:20, 10:20] = (255, 255, 255)  # white (lightning) is not the tracer
    x, y, n = measure_rotation.find_tracer(img)
    assert abs(x - 914.5) < 1 and abs(y - 509.5) < 1 and n == 600
    assert measure_rotation.find_tracer(np.zeros((100, 100, 3), np.uint8)) is None


def test_the_conversion_writes_the_plan_view_columns_and_keeps_track_times_absolute(tiny):
    root, p = tiny
    cm = p["colmax"]
    assert len(cm) == len(p["steps"]) and all((root / "clip" / m["bin"]).exists() for m in cm)
    assert (root / "clip" / cm[0]["bin"]).stat().st_size == cm[0]["nx"] * cm[0]["ny"]
    # a radiosonde track is in seconds after the first step already: the launch time is not added again
    for s in p["soundings"]:
        if s["drift"]:
            assert s["drift"][0][0] == pytest.approx(-600.0)


def test_the_couplet_summary_is_converted_with_its_verdict():
    raw = {
        "class": "retrieved",
        "survey_scale_estimate": SURVEY_ESTIMATE,
        "per_step": [
            {
                "t_s": 240.0,
                "best_radar": "NOXP",
                "best": {
                    "couplet": {
                        "vrot_ms": 7.9,
                        "couplet_diameter_km": 2.4,
                        "omega_s-1": 0.0066,
                        "period_s": 953.0,
                        "sense": "cyclonic",
                    },
                    "debris_indicator": {"voxels_flag2_near_rotation": 12},
                },
                "resolution": {
                    "effective_resolution_m": 500.0,
                    "tornado_resolved": False,
                    "verdict": "tornado vortex UNRESOLVED",
                },
            }
        ],
    }
    v = convert_bundle.convert_vortex(raw, lambda iso: 0.0)
    assert v["steps"][0]["tornado_resolved"] is False
    assert v["steps"][0]["effective_resolution_m"] == 500.0
    assert v["summary"]["tornado_resolved_any"] is False
    assert v["summary"]["vrot_ms_min_median_max"][1] == pytest.approx(7.9)
    assert convert_bundle.convert_vortex(None, lambda iso: 0.0) is None

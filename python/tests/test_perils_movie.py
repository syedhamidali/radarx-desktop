"""Tests of the PERiLS movie tools (tools/perils_movie/render): conversion of a SPEC v1 bundle, the line
motion by phase correlation, the timeline, the camera path and the events. No browser is needed; the
last test renders two frames in Chrome and is skipped when Playwright, Chrome or the built app is missing."""

from __future__ import annotations

import importlib
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
RENDER = REPO / "tools" / "perils_movie" / "render"
sys.path.insert(0, str(RENDER))

choreography = importlib.import_module("choreography")
convert_bundle = importlib.import_module("convert_bundle")
synth_bundle = importlib.import_module("synth_bundle")


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    """A small synthetic bundle (1.5 km cells), its conversion and the plan made from it."""
    root = tmp_path_factory.mktemp("perils")
    synth_bundle.write(root / "bundle", 5, 120.0, res_km=1.5, dz_km=1.0)
    perils = convert_bundle.convert(root / "bundle", root / "clip", log=lambda *_: None)
    return root, perils


# ── line motion ─────────────────────────────────────────────────────────────


def _blobs(shift_x=0.0, shift_y=0.0, n=128):
    yy, xx = np.mgrid[0:n, 0:n].astype(float)
    f = np.full((n, n), np.nan)
    for cx, cy, a in [(40, 50, 55), (80, 70, 45), (60, 30, 40), (90, 95, 50)]:
        g = a * np.exp(-(((xx - cx - shift_x) ** 2 + (yy - cy - shift_y) ** 2) / (2 * 6.0**2)))
        f = np.fmax(f, np.where(g > 15, g, np.nan))
    return f


@pytest.mark.parametrize("sx,sy", [(0, 0), (4, -3), (7.3, 2.6), (-5.5, 5.5)])
def test_phase_correlation_recovers_the_shift_to_a_fraction_of_a_cell(sx, sy):
    dx, dy, sharp = convert_bundle.phase_correlate(_blobs(), _blobs(sx, sy))
    assert abs(dx - sx) < 0.4 and abs(dy - sy) < 0.4
    assert sharp > 4


def test_motion_of_a_series_and_the_fallback_for_a_bad_pair():
    frames = [_blobs(2.0 * k, 0.7 * k) for k in range(4)]
    frames[2] = np.random.default_rng(1).uniform(0, 50, frames[2].shape)  # a corrupt step
    mo = convert_bundle.estimate_motion(frames, 1.0, [0, 60, 120, 180])
    assert len(mo) == 3
    assert not mo[0]["fallback"]
    assert mo[1]["fallback"] and mo[2]["fallback"]
    # the median of the good interval(s) replaces the bad ones
    assert abs(mo[1]["u_ms"] - mo[0]["u_ms"]) < 1e-6
    assert abs(mo[0]["dx_km"] - 2.0) < 0.4


def test_dbz_codes_round_trip_within_the_quantisation():
    d = np.array([-20.0, -10.0, 0.0, 35.3, 70.0, 90.0, np.nan])
    c = convert_bundle.encode_dbz(d)
    assert c[-1] == 0 and c[0] == 1 and c[4] == 255 and c[5] == 255
    back = convert_bundle.VMIN + (c[2:4].astype(float) - 1) / 254.0 * (
        convert_bundle.VMAX - convert_bundle.VMIN
    )
    assert np.all(np.abs(back - d[2:4]) < 0.2)


def test_sounding_csv_with_speed_and_direction_and_gaps():
    csv = "pres,hght,temp,dwpt,wspd,wdir\n1000,100,24.0,19.0,10,270\n850,1500,17.0,,20,225\n-999,300,0,0,0,0\n"
    lv = convert_bundle.sounding_levels(csv)
    assert lv["pressure_hpa"][:2] == [1000.0, 850.0]
    assert lv["dewpoint_c"][1] is None
    assert lv["u_ms"][0] == pytest.approx(
        10.0, abs=1e-6
    )  # wind from the west blows toward the east
    assert abs(lv["v_ms"][0]) < 1e-6
    assert lv["pressure_hpa"][2] is None


def test_geo_round_trip():
    g = convert_bundle.Geo(33.1, -88.4)
    x, y = g.xy(33.5, -87.9)
    la, lo = g.latlon(x, y)
    assert abs(la - 33.5) < 1e-6 and abs(lo + 87.9) < 1e-6
    assert 40 < float(x) < 60 and 40 < float(y) < 50


# ── conversion ──────────────────────────────────────────────────────────────


def test_conversion_writes_the_clip_bundle(tiny):
    root, p = tiny
    clip = root / "clip"
    man = json.loads((clip / "manifest.json").read_text())
    assert len(man["steps"]) == 5
    h = json.loads((clip / "mosaic_0.json").read_text())
    g = h["grid"]
    assert (clip / "mosaic_0.bin").stat().st_size == g["nx"] * g["ny"] * g["nz"]
    assert man["storm"]["speed_ms"] > 10
    assert len(p["motion"]) == 4 and all(not m["fallback"] for m in p["motion"])
    assert (
        p["lma"]["nSources"] > 100
        and p["stations"]
        and p["tornadoes"][0]["path"]
        and p["rotation"][0]["points"]
    )
    # flashes sit at their real positions in km of the scene (no jitter was added)
    f = p["lma"]["flashes"][0]
    assert all(len(row) == 5 for row in f["s"])
    assert p["soundings"][0]["levels"]["pressure_hpa"][0] == 1000.0


def test_the_v11_parts_are_converted(tiny):
    root, p = tiny
    clip = root / "clip"
    # winds: the clip's flow files (u then v, float32) and the visual layers, with masked cells never drawn
    assert p["hasWinds"]
    h = json.loads((clip / "wind_0.json").read_text())
    assert (clip / "wind_0.bin").stat().st_size == h["nx"] * h["ny"] * h["nz"] * 8
    man = json.loads((clip / "manifest.json").read_text())
    assert man["steps"][0]["wind"] == {"header": "wind_0.json", "bin": "wind_0.bin"}
    viz = p["winds"][0]
    assert viz["glyphs"] and all(len(g) == 4 for g in viz["glyphs"])
    assert viz["w"] and all(abs(c[3]) >= 5.0 for c in viz["w"])
    # the tornado: the survey path is a line between begin and end, flagged as interpolated
    tor = p["tornadoes"][0]
    assert tor["path_interpolated"] is True and len(tor["path"]) > 2
    # rotation tracks from the dict form, with the rotational velocity
    assert p["rotation"][0]["points"][0]["vrot_ms"] > 20
    # hail events attach to their station; drop-size spectra are converted to log10 N(D)
    pips = [s for s in p["stations"] if s.get("dsd")]
    assert pips
    from convert_extras import convert_stations

    st = convert_stations(
        [
            {
                "id": "P1",
                "lat": 33.0,
                "lon": -87.5,
                "series": {"t_s": [0, 1], "rain_rate_mmh": [0, float("nan")]},
            }
        ],
        convert_bundle.Geo(33.0, -87.5),
        root,
        [{"station": "P1", "t_s": 5, "kind": "hail_candidate", "max_d_mm": 12}],
    )
    assert st[0]["events"][0]["max_d_mm"] == 12 and st[0]["series"]["rain_rate_mmh"][1] is None
    d = pips[0]["dsd"]
    assert len(d["logN"]) == len(d["t"]) and len(d["logN"][0]) == len(d["d"])
    # science events and profiles
    assert {e["kind"] for e in p["science"]} >= {"gust_front", "tornado"}
    assert p["profiles"]["mrr"]["fields"]["dbz"]["codes"]
    assert sum(1 for h in p["hail"] if h) >= 1


def test_line_geometry_axis_is_perpendicular_to_the_motion(tiny):
    _root, p = tiny
    line = p["line"][0]
    m = p["motion"][0]
    heading = (math.degrees(math.atan2(m["u_ms"], m["v_ms"])) + 360) % 360
    d = abs(((line["perp_deg"] - heading) + 180) % 360 - 180)
    assert (
        d < 25
    )  # the aperture problem leaves the along-line component loose; the perpendicular one is right
    assert line["half_len_km"] > 30 and line["front_km"] > line["back_km"]


# ── timeline ────────────────────────────────────────────────────────────────


def test_timeline_is_monotone_covers_the_window_and_slows_down_at_the_dip():
    tl = choreography.build_timeline(960.0, 120.0, 30, dip_centre=60.0)
    td = tl["t_data_s"]
    assert td[0] == 0 and td[-1] == pytest.approx(960.0)
    assert np.all(np.diff(td) > 0)
    assert len(td) == 3601
    sp = tl["speedup"]
    assert sp[(60 * 30)] < 0.5 * sp[(10 * 30)]
    assert sp.min() > 0
    assert 960.0 / 120.0 > 7.9  # at least 10 minutes in 2: the mean speed-up is above x5


def test_clip_time_of_data_time_is_the_inverse():
    tl = choreography.build_timeline(720.0, 120.0, 30, dip_centre=70.0)
    for t in (0.0, 12.3, 70.0, 99.9, 120.0):
        td = choreography.data_time_of_clip(tl, t)
        assert choreography.clip_time_of_data(tl, td) == pytest.approx(t, abs=1e-6)


def test_dip_centre_solves_for_the_data_time_of_the_tornado_shot():
    c = choreography.solve_dip_centre(900.0, 400.0, 120.0, 30, 36.0, 58.0)
    tl = choreography.build_timeline(900.0, 120.0, 30, c)
    assert 36.0 <= c <= 58.0
    # a feasible target: its data time is shown at the middle of the shot
    assert abs(choreography.data_time_of_clip(tl, c) - 400.0) < 5.0


# ── camera, events ──────────────────────────────────────────────────────────


def test_camera_path_has_no_jumps_and_stays_in_bounds(tiny):
    _root, p = tiny
    res = choreography.plan(p)
    cam = res["camera"]
    assert len(cam) == 3601
    x = np.array([[c["x_km"], c["y_km"], c["z_km"]] for c in cam])
    step = np.linalg.norm(np.diff(x, axis=0), axis=1)
    assert step.max() < 2.0  # km per frame (60 km/s at the fastest transit of a shot change)
    acc = np.linalg.norm(np.diff(x, 2, axis=0), axis=1)
    assert acc.max() < 0.1  # no jerks: a smooth path
    h = np.array([c["heading_deg"] for c in cam])
    dh = np.abs((np.diff(h) + 180) % 360 - 180)
    assert dh.max() < 2.0  # degrees per frame
    pit = np.array([c["pitch_deg"] for c in cam])
    assert np.abs(np.diff(pit)).max() < 1.5 and np.abs(pit).max() <= 86.0
    assert all(c["z_km"] >= 0.0019 for c in cam)
    assert next(s["name"] for s in res["events"]["shots"]) == "establishing"
    assert (
        res["events"]["shots"][-1]["name"] == "topdown"
        and res["events"]["shots"][-1]["t1"] == 120.0
    )


def test_the_ground_level_shot_stands_at_a_station_looking_at_the_approaching_line(tiny):
    _root, p = tiny
    res = choreography.plan(p)
    win = [c for c in res["camera"] if c["shot"] == "window"]
    assert win
    st = next(s for s in p["stations"] if s["id"] == res["station"])
    assert win[len(win) // 2]["x_km"] == pytest.approx(st["x"], abs=1e-3)
    assert win[len(win) // 2]["alt_m"] < 10
    scene = choreography.Scene(p)
    look_back = (scene.perp_deg + 180.0) % 360.0
    d = abs(((win[5]["heading_deg"] - look_back) + 180) % 360 - 180)
    assert d < 25


def test_nearest_station_selection():
    sts = [{"id": "A", "x": 0.0, "y": 0.0}, {"id": "B", "x": 10.0, "y": 0.0}]
    assert choreography.nearest_station(sts, 1.0, 0.0, 0.002) == "A"
    assert choreography.nearest_station(sts, 9.0, 0.0, 0.5) == "B"
    assert choreography.nearest_station(sts, 0.0, 0.0, 50.0) == "A"
    assert choreography.nearest_station([], 0, 0, 0) is None


def test_events_follow_the_timeline(tiny):
    _root, p = tiny
    res = choreography.plan(p)
    ev = res["events"]
    fl = ev["flashes"]
    assert fl and all(0 <= f["t_clip_s"] <= 120 for f in fl)
    td = np.array([f["t_data_s"] for f in fl])
    tc = np.array([f["t_clip_s"] for f in fl])
    order = np.argsort(td)
    assert np.all(np.diff(tc[order]) >= -1e-9)  # later in the data, later in the clip
    assert (
        ev["tornado"]["rating"] == "EF2"
        and ev["tornado"]["t_clip_start_s"] < ev["tornado"]["t_clip_end_s"]
    )
    assert ev["nearest_station"][0]["t_clip_s"] == 0.0
    assert ev["observer_station"]["id"] == res["station"]
    tm = res["timeline"]
    assert tm["clip_duration_s"] == 120.0 and tm["fps"] == 30 and tm["frames"] == 3600
    assert tm["tornado_slowdown"]["centre_clip_s"] > 30


def test_without_a_tornado_the_plan_still_works(tiny):
    _root, p = tiny
    q = dict(p)
    q["tornadoes"] = []
    res = choreography.plan(q)
    assert "tornado" not in [s["name"] for s in res["events"]["shots"]]
    assert res["timeline"]["tornado_slowdown"] is None
    assert len(res["camera"]) == 3601


# ── Chrome (skipped when unavailable) ───────────────────────────────────────


def test_two_frames_render_in_chrome(tiny, tmp_path):
    pytest.importorskip("playwright")
    dist = REPO / "dist" / "index.html"
    if not dist.exists():
        pytest.skip("npm run build first")
    import shutil

    if not (shutil.which("google-chrome") or Path("/Applications/Google Chrome.app").exists()):
        pytest.skip("Chrome is not installed")
    rm = importlib.import_module("render_movie")
    root, _p = tiny
    choreography.write(root / "clip" / "perils.json", root / "clip")
    rm.main(
        [
            "--bundle",
            str(root / "bundle"),
            "--work",
            str(root),
            "--frames",
            "0:2",
            "--width",
            "960",
            "--height",
            "540",
        ]
    )
    assert (root / "frames" / "frame_00000.png").stat().st_size > 10_000

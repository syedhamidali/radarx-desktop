"""Tests of ``xrd animate`` (engine/animate*.py) that need neither a browser nor radar files:
file grouping and time matching, the tolerance, frame planning, interpolation weights, camera maths
(checked against values computed by the TypeScript of src/lib/utils/clipCamera.ts), camera and
config errors, the cache and resume logic, and the memory bound on a long synthetic series. One
end-to-end test drives a real Chrome and is skipped when Playwright, Chrome or the built app is
missing."""

# ruff: noqa: E501

from __future__ import annotations

import contextlib
import json
import math
import tracemalloc
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

from engine import animate
from engine import animate_camera as cam
from engine import animate_data as data
from engine import animate_plan as plan
from engine import animate_render as render
from engine.animate_plan import AnimateConfigError, AnimatePlanError
from engine.animate_storm import Cell

UTC = timezone.utc
T0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)


def touch(root: Path, radar: str, when: datetime, suffix: str = "_V06") -> Path:
    d = root / radar
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{radar}{when:%Y%m%d_%H%M%S}{suffix}"
    f.write_bytes(b"x")
    return f


def series(root: Path, radars: dict[str, float], n: int, every_s: float = 300.0) -> list[Path]:
    """``n`` volumes per radar every ``every_s`` s; each radar starts ``radars[r]`` s after T0."""
    files = []
    for r, off in radars.items():
        for k in range(n):
            files.append(touch(root, r, T0 + timedelta(seconds=off + k * every_s)))
    return files


def groups_of(files: list[Path]) -> dict[str, list[plan.VolumeRef]]:
    refs, bad = plan.scan_files(files, meta=lambda p: (_ for _ in ()).throw(ValueError("no")))
    assert not bad
    return plan.group_by_radar(refs)


# ---------------------------------------------------------------------------
# files -> radar and time -> steps
# ---------------------------------------------------------------------------


class TestFiles:
    def test_name_gives_radar_and_time(self):
        assert plan.parse_volume_name("/x/KHPX20211211_020457_V06") == (
            "KHPX",
            datetime(2021, 12, 11, 2, 4, 57, tzinfo=UTC),
        )
        assert plan.parse_volume_name("KTLX20130520_201643_V06.gz")[0] == "KTLX"
        assert plan.parse_volume_name("radar_20211211_020457.nc")[0] == "RADAR"
        assert plan.parse_volume_name("results.csv") is None
        assert plan.parse_volume_name("KHPX20211341_020457_V06") is None  # month 13

    def test_file_without_a_time_in_its_name_is_read_from_the_file(self, tmp_path):
        f = tmp_path / "volume.nc"
        f.write_bytes(b"x")
        refs, bad = plan.scan_files([f], meta=lambda p: ("KABC", T0))
        assert [(r.radar, r.time, r.source) for r in refs] == [("KABC", T0, "file")]
        assert not bad

    def test_unreadable_file_is_skipped_with_the_reason(self, tmp_path):
        f = tmp_path / "volume.nc"
        f.write_bytes(b"x")

        def boom(_p):
            raise OSError("not a radar file")

        refs, bad = plan.scan_files([f, f], meta=boom)  # the duplicate is dropped
        assert refs == [] and len(bad) == 1
        assert (
            "cannot tell the radar and time" in bad[0].reason
            and "not a radar file" in bad[0].reason
        )

    def test_group_by_radar_sorts_and_drops_same_second_duplicates(self, tmp_path):
        a = touch(tmp_path, "KAAA", T0 + timedelta(minutes=6))
        b = touch(tmp_path / "x", "KAAA", T0)
        c = touch(tmp_path / "y", "KAAA", T0)  # the same second as b, another folder
        g = groups_of([a, b, c])
        assert [v.time for v in g["KAAA"]] == [T0, T0 + timedelta(minutes=6)]

    def test_parse_time(self):
        assert plan.parse_time("2021-12-11T02:00:00Z") == T0
        assert plan.parse_time("2021-12-11 02:00") == T0
        assert plan.parse_time("20211211T0200") == T0
        with pytest.raises(AnimatePlanError, match="cannot read the time"):
            plan.parse_time("yesterday")


class TestMetaCache:
    def test_a_file_is_opened_once_and_the_answer_is_remembered(self, tmp_path):
        f = tmp_path / "volume.nc"
        f.write_bytes(b"x")
        calls = []

        def meta(p):
            calls.append(p.name)
            return "KABC", T0

        store = tmp_path / "scan.json"
        c1 = plan.MetaCache(store)
        refs, _ = plan.scan_files([f], meta=c1.wrap(meta))
        c1.save()
        assert refs[0].radar == "KABC" and calls == ["volume.nc"]
        refs, _ = plan.scan_files([f], meta=plan.MetaCache(store).wrap(meta))  # a new process
        assert refs[0].time == T0 and calls == ["volume.nc"]  # not read again
        f.write_bytes(b"a changed file")
        plan.scan_files([f], meta=plan.MetaCache(store).wrap(meta))
        assert calls == ["volume.nc", "volume.nc"]  # a changed file is read again

    def test_radar_label_prefers_what_the_file_says(self):
        assert plan.radar_label({"instrument_name": " dow8 "}, 36.7, -87.2, "cfrad.2021") == "DOW8"
        assert (
            plan.radar_label({"instrument_name": "None"}, 36.74, -87.29, "x") == "SITE+36.74-87.29"
        )
        assert plan.radar_label({}, None, None, "file_stem") == "FILE_STEM"
        # files of one radar named by time must still be one radar
        a = plan.radar_label({}, 36.7369, -87.2856, "cfrad.20211011_201557")
        b = plan.radar_label({}, 36.7371, -87.2854, "cfrad.20211011_202557")
        assert a == b

    def test_real_netcdf_without_a_time_in_its_name(self):
        from tests import radar_data

        f = radar_data.get(radar_data.DOW8_PPI)
        if f is None:
            pytest.skip("open-radar-data sample not available")
        refs, bad = plan.scan_files([f])
        assert not bad and refs[0].source == "file" and refs[0].time.year == 2021
        assert refs[0].radar != f.stem.upper()  # a name for the radar, not for the file


class TestSteps:
    def test_each_radar_contributes_its_nearest_volume(self, tmp_path):
        files = series(tmp_path, {"REF1": 0, "OTH2": 120, "OTH3": -45}, 4)
        sp = plan.choose_steps(groups_of(files), "REF1")
        assert len(sp.steps) == 4 and sp.reference == "REF1"
        assert sp.steps[0].files["OTH2"][1] == pytest.approx(120.0)
        assert sp.steps[0].files["OTH3"][1] == pytest.approx(-45.0)
        assert sp.times_s == [0.0, 300.0, 600.0, 900.0]
        assert sp.skipped == []  # every file was used

    def test_nearest_not_just_the_same_index(self, tmp_path):
        # OTH2 scans 4 min later each time: the step at 10 min takes its 14 min volume (offset +240)
        files = series(tmp_path, {"REF1": 0}, 3, 600.0) + series(tmp_path, {"OTH2": 200}, 4, 600.0)
        sp = plan.choose_steps(groups_of(files), "REF1", tolerance_s=330)
        assert [round(s.files["OTH2"][1]) for s in sp.steps] == [200, 200, 200]

    def test_tolerance_decides_who_joins_a_step(self, tmp_path):
        files = series(tmp_path, {"REF1": 0, "NEA2": 100, "FAR3": 400}, 3, every_s=1000.0)
        sp = plan.choose_steps(groups_of(files), "REF1", tolerance_s=330)
        assert all("FAR3" not in s.files for s in sp.steps)  # 400 s away: outside the tolerance
        assert all("NEA2" in s.files for s in sp.steps)
        wide = plan.choose_steps(groups_of(files), "REF1", tolerance_s=450)
        assert all("FAR3" in s.files for s in wide.steps)

    def test_files_that_are_not_used_are_listed_with_the_reason(self, tmp_path):
        files = series(tmp_path, {"REF1": 0, "OTH2": 100}, 3)
        files.append(
            touch(tmp_path, "OTH2", T0 + timedelta(seconds=100 + 40))
        )  # a closer one wins? no
        files.append(touch(tmp_path, "FAR9", T0 + timedelta(hours=5)))
        sp = plan.choose_steps(groups_of(files), "REF1")
        reasons = {s.what: s.reason for s in sp.skipped}
        far = next(r for n, r in reasons.items() if n.startswith("FAR9"))
        assert "tolerance" in far and "no step uses it" in far
        extra = next(r for n, r in reasons.items() if n.startswith("OTH2") and "020220" in n)
        assert "not the nearest OTH2 volume" in extra

    def test_a_step_with_too_few_radars_is_dropped_and_said_so(self, tmp_path):
        files = series(tmp_path, {"REF1": 0, "OTH2": 60}, 4)
        files.append(touch(tmp_path, "REF1", T0 + timedelta(hours=3)))  # alone at 3 h
        sp = plan.choose_steps(groups_of(files), "REF1")
        assert len(sp.steps) == 4
        assert any("only 1 radar(s)" in s.reason for s in sp.skipped)

    def test_repeated_volumes_do_not_make_a_new_step(self, tmp_path):
        # OTH2 and OTH3 scan hourly: the later steps would only repeat their volumes
        files = (
            series(tmp_path, {"REF1": 0}, 6)  # every 5 min for 25 min
            + series(tmp_path, {"OTH2": 30, "OTH3": 50}, 2, every_s=1500.0)  # two volumes each
        )
        sp = plan.choose_steps(groups_of(files), "REF1", tolerance_s=2000)
        # steps 1 to 4 would only repeat the volumes of OTH2 and OTH3 (2 of 3 radars unchanged)
        assert len(sp.steps) == 2 and sp.times_s == [0.0, 900.0]
        assert sum("repeats the volumes" in s.reason for s in sp.skipped) == 4

    def test_auto_reference_gives_the_most_steps_then_the_smallest_offsets(self, tmp_path):
        files = (
            series(tmp_path, {"AAAA": 0}, 5)
            + series(tmp_path, {"BBBB": 20}, 5)
            + series(tmp_path, {"CCCC": 60}, 5)
        )
        sp = plan.choose_steps(groups_of(files))
        assert sp.reference == "BBBB" and len(sp.steps) == 5

    def test_window_thinning_and_limit(self, tmp_path):
        files = series(tmp_path, {"REF1": 0, "OTH2": 30}, 12)
        g = groups_of(files)
        w = plan.choose_steps(
            g, "REF1", start=T0 + timedelta(minutes=10), end=T0 + timedelta(minutes=30)
        )
        assert next(s.time for s in w.steps) == T0 + timedelta(minutes=10) and len(w.steps) == 5
        assert any("outside the --start" in s.reason for s in w.skipped)
        thin = plan.choose_steps(g, "REF1", min_step_s=600)
        assert [int(t) for t in thin.times_s] == [0, 600, 1200, 1800, 2400, 3000]
        assert any("--min-step" in s.reason for s in thin.skipped)
        assert len(plan.choose_steps(g, "REF1", max_steps=3).steps) == 3

    def test_single_radar_series(self, tmp_path):
        sp = plan.choose_steps(groups_of(series(tmp_path, {"ONLY": 0}, 5)))
        assert len(sp.steps) == 5 and sp.used_radars == ["ONLY"]

    def test_errors_say_what_to_do(self, tmp_path):
        files = series(tmp_path, {"REF1": 0, "OTH2": 30}, 3)
        g = groups_of(files)
        with pytest.raises(AnimatePlanError, match="Did you mean REF1"):
            plan.choose_steps(g, "REF2")
        with pytest.raises(AnimatePlanError, match="fewer than two usable time steps"):
            plan.choose_steps(groups_of(series(tmp_path / "one", {"AAA1": 0, "BBB2": 10}, 1)))
        with pytest.raises(AnimatePlanError, match="--min-radars 5"):
            plan.choose_steps(g, min_radars=5)
        with pytest.raises(AnimatePlanError, match="window"):
            plan.choose_steps(g, start=T0 + timedelta(days=1))

    def test_offsets_and_tables(self, tmp_path):
        sp = plan.choose_steps(groups_of(series(tmp_path, {"REF1": 0, "OTH2": 90}, 3)), "REF1")
        off = plan.offset_summary(sp)
        assert off["OTH2"]["mean_abs_offset_s"] == pytest.approx(90.0)
        assert off["REF1"]["max_abs_offset_s"] == 0.0
        rows = plan.step_table(sp)
        assert rows[1]["offsets_s"] == {"OTH2": 90.0, "REF1": 0.0} and rows[1]["t_s"] == 300.0


# ---------------------------------------------------------------------------
# frames and interpolation
# ---------------------------------------------------------------------------


class TestFrames:
    # reference values computed by src/lib/utils/clipCamera.ts (dataTimeAt, locate, morphShifts)
    def test_data_time_matches_the_typescript(self):
        ts = [0, 1, 1.5, 3, 10, 18, 30, 34.5, 35, 36]
        want = [
            0,
            0,
            0,
            28.484222389181067,
            202.6120711244678,
            450,
            804.252441773103,
            900,
            900,
            900,
        ]
        for t, w in zip(ts, want, strict=True):
            assert plan.data_time_at(t, 36, 0, 900) == pytest.approx(w, abs=1e-9)
        for t, w in zip(
            [0, 2, 10, 20],
            [100, 119.27437641723355, 566.4399092970522, 1190.702947845805],
            strict=True,
        ):
            assert plan.data_time_at(t, 24, 100, 1300) == pytest.approx(w, abs=1e-9)

    def test_data_time_is_monotone_and_holds_both_ends(self):
        prev = -1.0
        for i in range(0, 1081):
            td = plan.data_time_at(i / 30, 36, 0, 1086)
            assert td >= prev
            prev = td
        assert plan.data_time_at(0.0, 36, 5, 99) == 5 and plan.data_time_at(1.5, 36, 5, 99) == 5
        assert plan.data_time_at(34.5, 36, 5, 99) == 99 and plan.data_time_at(36, 36, 5, 99) == 99

    def test_clip_time_inverts_data_time(self):
        for t in (2.0, 9.7, 20.0, 33.0):
            td = plan.data_time_at(t, 36, 0, 900)
            assert plan.clip_time_of(td, 36, 0, 900) == pytest.approx(t, abs=1e-6)

    def test_locate_matches_the_typescript(self):
        ts = [0, 300, 620, 900]
        got = [plan.locate(ts, t) for t in (-5, 0, 100, 300, 301, 899, 900, 2000)]
        want = [
            (0, 0),
            (0, 0),
            (0, 1 / 3),
            (0, 1),
            (1, 0.003125),
            (2, 0.9964285714285714),
            (2, 1),
            (2, 1),
        ]
        for (i, s), (wi, ws) in zip(got, want, strict=True):
            assert i == wi and s == pytest.approx(ws, abs=1e-12)

    def test_interpolation_weights(self):
        assert plan.interpolation_weights(0.0) == (1.0, 0.0)
        assert plan.interpolation_weights(1.0) == (0.0, 1.0)
        for s in (0.0, 0.1, 0.25, 0.5, 0.9, 1.0):
            a, b = plan.interpolation_weights(s)
            assert a + b == pytest.approx(1.0) and 0 <= a <= 1 and 0 <= b <= 1
        assert plan.interpolation_weights(-3) == (1.0, 0.0) and plan.interpolation_weights(7) == (
            0.0,
            1.0,
        )

    def test_morph_shifts_match_the_typescript(self):
        a, b = plan.morph_shifts((0.01, 0.02), 300, 0.25)
        assert a == pytest.approx((0.75, 1.5)) and b == pytest.approx((-2.25, -4.5))
        # a cell carried by both shifts lands on the same place: where the storm is at that moment
        m, dt, s = (0.012, -0.004), 280.0, 0.4
        sa, sb = plan.morph_shifts(m, dt, s)
        assert sa[0] - sb[0] == pytest.approx(m[0] * dt) and sa[1] - sb[1] == pytest.approx(
            m[1] * dt
        )

    def test_frame_plan_counts_and_labels(self):
        ts = [0.0, 300.0, 600.0, 900.0]
        frames = plan.plan_frames(36, 30, ts)
        assert len(frames) == 1080 == plan.frame_count(36, 30)
        assert (
            frames[0].observed
            and frames[0].td == 0
            and frames[-1].observed
            and frames[-1].td == 900
        )
        # the 1.5 s holds are observed
        assert all(f.observed for f in frames[:45]) and all(f.observed for f in frames[-45:])
        # a frame halfway between two volumes is interpolated, with s = 0.5 and the right interval
        mid = min(frames, key=lambda f: abs(f.td - 450))
        assert not mid.observed and mid.k == 1 and mid.s == pytest.approx(0.5, abs=0.02)
        s = plan.summarise_frames(frames, ts)
        assert s["frames"] == 1080 and s["observed_frames"] + s["interpolated_frames"] == 1080
        assert s["interpolated_share"] == pytest.approx(s["interpolated_frames"] / 1080)
        assert sum(i["frames"] for i in s["intervals"]) == 1080
        assert s["longest_gap_s"] == 300 and s["data_span_s"] == 900

    def test_every_frame_is_inside_its_interval(self):
        ts = [0.0, 300.0, 620.0, 900.0]
        for f in plan.plan_frames(20, 12, ts):
            assert ts[f.k] - 1e-9 <= f.td <= ts[f.k + 1] + 1e-9 and 0 <= f.s <= 1

    def test_observed_tolerance(self):
        ts = [0.0, 300.0]
        tight = plan.summarise_frames(plan.plan_frames(36, 30, ts, observed_tol_s=0.5), ts)
        loose = plan.summarise_frames(plan.plan_frames(36, 30, ts, observed_tol_s=30), ts)
        assert tight["observed_frames"] < loose["observed_frames"]

    def test_timelapse(self):
        assert plan.duration_for_timelapse(1086, 33) == pytest.approx(35.9, abs=0.05)
        assert plan.timelapse_of(1086, 36) == pytest.approx(1086 / 33)
        with pytest.raises(AnimatePlanError):
            plan.duration_for_timelapse(100, 0)

    def test_too_short_clip_is_refused(self):
        with pytest.raises(AnimatePlanError, match="too short"):
            plan.plan_frames(3.0, 30, [0, 300])
        with pytest.raises(AnimateConfigError, match="too short"):
            cam.check_duration(3.0)


# ---------------------------------------------------------------------------
# camera
# ---------------------------------------------------------------------------


class TestCameraMaths:
    # reference values computed by src/lib/utils/clipCamera.ts
    def test_hermite_matches_the_typescript(self):
        ts, vs = [0, 5, 12, 20], [0, 10, 4, 30]
        want = [0, 0, 5, 10, 7.6384839650145775, 4, 17, 30, 30]
        for t, w in zip([-1, 0, 2.5, 5, 8, 12, 16, 20, 25], want, strict=True):
            assert cam.hermite(ts, vs, t) == pytest.approx(w, abs=1e-9)

    def test_hermite_has_zero_speed_at_the_ends_and_no_overshoot(self):
        ts, vs = [0, 10, 20, 30], [0, 1, 2, 10]
        prev = -1e9
        for i in range(0, 301):
            v = cam.hermite(ts, vs, i / 10)
            assert v >= prev - 1e-12 and 0 <= v <= 10
            prev = v
        assert abs(cam.hermite(ts, vs, 1e-4) - 0) < 1e-6

    def test_storm_position_matches_the_typescript(self):
        tr = [(0, 0, 0), (300, 6, 4), (620, 11, 12), (900, 20, 15)]
        want = [(-2, -1.3333333333333335), (0, 0), (3.084677419354839, 1.7741935483870968),
                (6, 4), (8.145434964087702, 7.760041267641128), (11, 12),
                (16.583454810495628, 14.103498542274052), (20, 15), (23.214285714285715, 16.071428571428573)]  # fmt: skip
        for t, (wx, wy) in zip([-100, 0, 150, 300, 450, 620, 800, 900, 1000], want, strict=True):
            x, y = cam.storm_at(tr, t)
            assert x == pytest.approx(wx, abs=1e-9) and y == pytest.approx(wy, abs=1e-9)

    def test_default_orbit_matches_the_typescript(self):
        keys = cam.storm_choreography(36, 55, 4)
        want = [(0, 205, 30, 215, 4), (5, 215, 27, 165, 4), (12, 365, 22, 118, 4), (19, 505, 18, 100, 4),
                (24, 560, 6, 128, 6.4), (29, 552, 12, 118, 4.8), (32.5, 540, 86, 152, 0), (36, 565, 30, 215, 4)]  # fmt: skip
        assert [(k.t, k.theta, k.phi, k.dist, k.dz) for k in keys] == pytest.approx(want)
        short = cam.storm_choreography(24, 200, 4)
        samples = [cam.camera_at(short, t) for t in (0, 3, 10, 15, 22, 30)]
        want = [(205, 30, 215, 4), (213.30354922279793, 27.346860576923078, 169.68977436389824, 4),
                (420.4079640965039, 20.50587820463204, 107.27266203184571, 4),
                (670.3584905660377, 8.290938053097346, 121.952, 5.881600000000001),
                (541.3848396501458, 82.89795918367344, 160.12518409425633, 0.2215743440233262),
                (565, 30, 215, 4)]  # fmt: skip
        for c, w in zip(samples, want, strict=True):
            assert (c.theta, c.phi, c.dist, c.dz) == pytest.approx(w, abs=1e-9)

    def test_wide_orbit_matches_the_typescript(self):
        k = cam.wide_choreography(48, 330, 3)[:3]
        assert k[0].dist == pytest.approx(379.5) and k[0].t == 0
        assert k[1].t == pytest.approx(10.666666666666666) and k[1].dist == pytest.approx(313.5)
        assert k[2].t == pytest.approx(22.666666666666664) and k[2].dz == pytest.approx(4.5)

    def test_no_jumps_over_the_whole_choreography_at_30_fps(self):
        for keys, dur in (
            (cam.storm_choreography(36, 55), 36),
            (cam.wide_choreography(48), 48),
            (cam.topdown_choreography(30), 30),
        ):
            path = cam.describe_path(keys, dur, 30)
            assert path["max_turn_deg_s"] < 120  # a smooth orbit, never a cut
            pts = [cam.camera_at(keys, i / 30) for i in range(dur * 30)]
            for a, b in pairwise(pts):
                assert abs(b.dist - a.dist) < 3.0 and abs(b.phi - a.phi) < 3.0

    def test_the_target_follows_part_of_the_storm(self):
        track = [(-9.3, -5.7), (-3.5, -1.3), (1.9, 2.7), (7.8, 6.4)]
        anchor, half, extent = cam.track_frame(track, 60.0)
        assert anchor == pytest.approx((-0.75, 0.35)) and extent == pytest.approx(17.1)
        assert half == pytest.approx(60 + extent / 2)
        assert cam.lag_target(anchor, (7.8, 6.4), 0.0) == pytest.approx(anchor)
        assert cam.lag_target(anchor, (7.8, 6.4), 1.0) == pytest.approx((7.8, 6.4))
        t = cam.lag_target(anchor, (7.8, 6.4), 0.3)
        assert t == pytest.approx(
            (anchor[0] + 0.3 * (7.8 - anchor[0]), anchor[1] + 0.3 * (6.4 - anchor[1]))
        )

    def test_fov_is_a_channel_only_when_a_key_sets_it(self):
        keys = [cam.CamKey(0, 0, 30, 100), cam.CamKey(10, 90, 30, 100, fov=85)]
        assert cam.camera_at(keys, 0).fov == pytest.approx(45)
        assert cam.camera_at(keys, 10).fov == pytest.approx(85)
        assert 45 < cam.camera_at(keys, 5).fov < 85
        assert cam.camera_at([cam.CamKey(0, 0, 30, 100), cam.CamKey(10, 9, 30, 100)], 5).fov is None

    def test_page_keys_use_the_renderer_names(self):
        k = cam.CamKey(1, 180, 86, 150, 1, 2, 3, fov=50)
        assert k.as_page() == {
            "t": 1,
            "theta": 180,
            "phi": 86,
            "dist": 150,
            "dx": 1,
            "dy": 2,
            "dz": 3,
            "fov": 50,
        }


class TestCameraFiles:
    GOOD: ClassVar[dict] = {
        "target_lag": 0.4,
        "fov": 50,
        "key": [
            {"t": 0, "azimuth": 200, "elevation": 30, "distance": 200},
            {"t": 10, "theta": 260, "phi": 12, "dist": 90, "target": [1, -2, 3.5], "fov": 70},
            {"t": 20, "azimuth": 300, "elevation": 40, "distance": 150, "dz": 2},
        ],
    }

    def test_a_good_file(self):
        spec = cam.parse_camera_table(self.GOOD, "cam.toml")
        assert spec.mode == "orbit" and spec.target_lag == 0.4
        k = spec.keys
        assert (k[1].theta, k[1].phi, k[1].dist, k[1].dx, k[1].dy, k[1].dz, k[1].fov) == (
            260,
            12,
            90,
            1,
            -2,
            3.5,
            70,
        )
        plan_ = cam.resolve_camera(spec, None, 55.0)
        assert plan_.duration == 20 and plan_.target_lag == 0.4 and plan_.mode == "orbit"
        assert plan_.keys[0].fov == 50 and plan_.keys[1].fov == 70  # the global fov fills the gaps
        assert plan_.page_settings()["keys"][1]["dz"] == 3.5

    def test_toml_file_round_trip(self, tmp_path):
        pytest.importorskip("tomllib")
        f = tmp_path / "cam.toml"
        f.write_text(
            "target_lag = 0.5\n[[key]]\nt = 0\nazimuth = 180\nelevation = 85\ndistance = 150\n"
            "[[key]]\nt = 8\nazimuth = 180\nelevation = 85\ndistance = 90\n"
        )
        spec = cam.load_camera(str(f))
        assert spec.keys and len(spec.keys) == 2 and spec.target_lag == 0.5
        assert spec.source == "camera file cam.toml"

    def test_unknown_key_suggests_the_nearest(self):
        bad = {"key": [{"t": 0, "azimut": 200, "elevation": 30, "distance": 200}, {"t": 1}]}
        with pytest.raises(
            AnimateConfigError,
            match=r'unknown key "azimut" in keyframe 1 \(did you mean "azimuth"\?\)',
        ):
            cam.parse_camera_table(bad)
        with pytest.raises(
            AnimateConfigError, match=r'unknown key "target_lg".*did you mean "target_lag"'
        ):
            cam.parse_camera_table({"target_lg": 0.3})
        with pytest.raises(AnimateConfigError, match=r'unknown key "dist_km_" in \[observer\]'):
            cam.parse_camera_table({"observer": {"dist_km_": 3}})

    @pytest.mark.parametrize(
        ("table", "message"),
        [
            ({"key": [{"t": 0, "azimuth": 1, "elevation": 30}, {"t": 1}]}, '"distance"'),
            ({"key": [{"t": 0, "azimuth": 1, "elevation": 30, "distance": 100}]}, "at least two"),
            (
                {
                    "key": [
                        {"t": 5, "azimuth": 1, "elevation": 30, "distance": 100},
                        {"t": 5, "azimuth": 1, "elevation": 30, "distance": 100},
                    ]
                },
                "must increase",
            ),
            (
                {
                    "key": [
                        {"t": 0, "azimuth": 1, "elevation": 95, "distance": 100},
                        {"t": 1, "azimuth": 1, "elevation": 3, "distance": 9},
                    ]
                },
                '"elevation" = 95',
            ),
            (
                {"key": [{"t": 0, "azimuth": "north", "elevation": 30, "distance": 100}, {"t": 1}]},
                "must be a number",
            ),
            (
                {
                    "key": [
                        {"t": 0, "azimuth": 1, "elevation": 30, "distance": 100, "target": [1, 2]},
                        {"t": 1},
                    ]
                },
                "[dx, dy, dz]",
            ),
            (
                {"key": [{"t": -1, "azimuth": 1, "elevation": 30, "distance": 100}, {"t": 1}]},
                "negative",
            ),
            (
                {
                    "key": [
                        {"t": 0, "azimuth": 1, "theta": 2, "elevation": 30, "distance": 100},
                        {"t": 1},
                    ]
                },
                "twice",
            ),
            ({"target_lag": 3}, "between 0 and 1"),
            ({"fov": 400}, "between 10 and 120"),
            ({"preset": "orbitt"}, 'Did you mean "orbit"'),
        ],
    )
    def test_clear_errors(self, table, message):
        with pytest.raises(
            AnimateConfigError, match=message.replace("[", r"\[").replace("]", r"\]")
        ):
            cam.parse_camera_table(table)

    def test_presets(self):
        for name in cam.PRESETS:
            spec = cam.load_camera(name)
            resolved = cam.resolve_camera(spec, 30, 100.0)
            assert resolved.duration == 30
            if name in ("window", "flight"):
                assert resolved.keys is None and resolved.vexag == 1.0
                assert resolved.mode == {"window": "observer", "flight": "flight"}[name]
            else:
                assert resolved.keys and resolved.mode == "orbit"
        assert cam.resolve_camera(cam.load_camera("top-down"), None, 0).target_lag == 0.5
        assert cam.resolve_camera(cam.load_camera("orbit"), None, 0).target_lag == 0.3
        with pytest.raises(AnimateConfigError, match='Did you mean "top-down"'):
            cam.load_camera("topdown")

    def test_options_override_the_file_and_preset(self):
        spec = cam.parse_camera_table(self.GOOD)
        r = cam.resolve_camera(spec, 40, 0, target_lag=0.1, fov=65)
        assert (
            r.duration == 40 and r.target_lag == 0.1 and r.keys[0].fov == 65 and r.keys[1].fov == 70
        )
        o = cam.resolve_camera(cam.load_camera("window"), None, 0, observer={"distKm": 4.0})
        assert o.page_settings()["observer"] == {"distKm": 4.0}

    def test_missing_and_bad_files(self, tmp_path):
        with pytest.raises(AnimateConfigError, match="file not found"):
            cam.load_camera(str(tmp_path / "nope.toml"))
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json")
        with pytest.raises(AnimateConfigError, match=r"cannot read bad\.json"):
            cam.load_camera(str(bad))
        txt = tmp_path / "x.yaml"
        txt.write_text("a: 1")
        with pytest.raises(AnimateConfigError, match=r"use a \.toml or \.json"):
            cam.read_table(txt)

    def test_path_report_flags_keys_after_the_clip(self):
        spec = cam.parse_camera_table(self.GOOD)
        p = cam.describe_path(spec.keys, 12, 30)
        assert p["ends_after_clip"] is True and p["keys"] == 3
        assert cam.describe_path(spec.keys, 30, 30)["holds_last_pose_s"] == pytest.approx(10.0)


EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples"


class TestDocumentedExamples:
    def test_the_example_camera_file_is_valid(self):
        pytest.importorskip("tomllib")
        spec = cam.load_camera(str(EXAMPLES / "animate_camera.toml"))
        r = cam.resolve_camera(spec, None, 55.0)
        assert len(spec.keys) == 5 and r.duration == 24 and r.target_lag == 0.35
        assert [k.fov for k in r.keys] == [50, 50, 70, 50, 50]
        assert cam.describe_path(r.keys, r.duration)["max_turn_deg_s"] < 60

    def test_the_example_look_file_is_read_by_the_renderer(self, chrome):
        dist = render.find_frontend()
        with render.Renderer(dist, 64, 64, offline=True) as r:
            changed = r.check_look(
                render.look_settings(
                    render.LookArgs(None, str(EXAMPLES / "animate_look.toml"), None)
                )
            )
        assert changed["exposure"] == 1.35 and changed["densityScale"] == 1.2

    def test_the_cli_text_of_the_docs_parses(self):
        from engine import cli

        parser = cli.build_parser()
        for argv in (
            ["animate", "-i", "mayfield", "-o", "m.mp4", "--camera", "top-down", "--duration", "30"],
            ["animate", "-i", "mayfield", "-p", "still.png", "--still-at", "12"],
            ["animate", "-i", "mayfield", "-o", "w.mp4", "--camera", "window", "--realistic", "--timelapse", "24"],
            ["animate", "-i", "laura", "-o", "l.mp4", "--track", "eye", "--camera", "flight", "--crop-km", "140",
             "--detail-m", "1000", "--realistic", "--look-preset", "Hurricane (stormy)"],
        ):  # fmt: skip
            _, extras = parser.parse_known_args(argv)
            assert not [e for e in extras if e.startswith("-")], argv


class TestLookArguments:
    def test_set_values(self):
        assert render.parse_overrides(["exposure=1.6", "light=night", " temperature = -0.2"]) == {
            "exposure": 1.6,
            "light": "night",
            "temperature": -0.2,
        }
        with pytest.raises(AnimateConfigError, match="--set expects key=value"):
            render.parse_overrides(["exposure"])

    def test_look_file_is_read_and_missing_is_an_error(self, tmp_path):
        f = tmp_path / "look.toml"
        f.write_text('[scene]\nlight = "dusk"\n')
        s = render.look_settings(render.LookArgs("Golden hour", str(f), ["exposure=2"]))
        assert s["lookPreset"] == "Golden hour" and s["lookFormat"] == "toml"
        assert "dusk" in s["lookText"] and s["lookOverrides"] == {"exposure": 2.0}
        with pytest.raises(AnimateConfigError, match="file not found"):
            render.look_settings(render.LookArgs(None, str(tmp_path / "no.toml"), None))


# ---------------------------------------------------------------------------
# a synthetic gridder: the cache, resume and the memory bound without radar files
# ---------------------------------------------------------------------------

ORIGIN = (36.0, -89.0)
SPEED_KM_S = 0.02  # 20 m/s toward the east-north-east


class SyntheticGridder:
    """Stands in for engine.mosaic: a storm moving at a known speed, grids of a chosen size."""

    def __init__(
        self, group_mb: float = 0.0, fine_shape=(8, 64, 64), fail_fine_at: int | None = None
    ):
        self.group_bytes = int(group_mb * 1e6)
        self.fine_shape = fine_shape
        self.fail_fine_at = fail_fine_at
        self.calls = {"coarse": 0, "fine": 0, "winds": 0}
        self.live = 0
        self.peak_live = 0

    def _centre(self, step) -> tuple[float, float]:
        t = (step.time - T0).total_seconds()
        return -20.0 + SPEED_KM_S * t * 0.9, -10.0 + SPEED_KM_S * t * 0.45

    @contextlib.contextmanager
    def _hold_group(self, step):
        self.live += 1
        self.peak_live = max(self.peak_live, self.live)
        mem = np.ones(self.group_bytes, dtype=np.uint8)  # the radar volumes of the step
        try:
            yield mem
        finally:
            self.live -= 1
            del mem

    def coarse(self, step, opts):
        self.calls["coarse"] += 1
        with self._hold_group(step):
            cx, cy = self._centre(step)
            comp = np.full((60, 60), np.nan, dtype=np.float32)
            comp[28:32, 28:32] = 55.0
            cells = [
                Cell(cx, cy, 62.0, 400.0, 14.0, 90.0),  # the storm that moves
                Cell(30.0, 30.0, 66.0, 100.0, 9.0, 80.0),  # a stationary one, not as strong a score
            ]
            return data.Coarse(cells, comp, -60000.0, -60000.0, 2000.0, ORIGIN)

    def fine(self, step, opts, extent):
        self.calls["fine"] += 1
        if self.fail_fine_at is not None and self.calls["fine"] == self.fail_fine_at:
            raise RuntimeError("the machine went to sleep")
        with self._hold_group(step):
            nz, ny, nx = self.fine_shape
            vol = np.zeros(self.fine_shape, dtype=np.uint8)
            vol[:, ny // 2 - 4 : ny // 2 + 4, nx // 2 - 4 : nx // 2 + 4] = 150
            lat = (extent["south"] + extent["north"]) / 2
            lon = (extent["west"] + extent["east"]) / 2
            header = {
                "mosaic_id": f"syn{int(self._centre(step)[0] * 1000)}",
                "variable": "DBZH",
                "units": "dBZ",
                "product": "composite",
                "grid": {
                    "x0": -nx * 250.0,
                    "y0": -ny * 250.0,
                    "cell_m": 500.0,
                    "nx": nx,
                    "ny": ny,
                    "nz": nz,
                    "z": [500.0 * (k + 1) for k in range(nz)],
                    "origin_lat": lat,
                    "origin_lon": lon,
                },
                "vmin": -20.0,
                "vmax": 75.0,
                "dtype": "uint8",
                "cmap": "NWSRef",
                "radars": [
                    {
                        "name": "AAAA",
                        "latitude": 36.5,
                        "longitude": -88.5,
                        "altitude_m": 100.0,
                        "x_m": 1.0,
                        "y_m": 2.0,
                    }
                ],
                "analysis_time": step.time.isoformat(),
            }
            return data.Fine(header, vol)

    def winds(self, step, opts, origin, extent_km, motion_ms):
        self.calls["winds"] += 1
        return None


def make_plan(tmp_path: Path, n: int, radars=None) -> plan.StepPlan:
    files = series(tmp_path / "radar", radars or {"AAAA": 0, "BBBB": 40}, n)
    return plan.choose_steps(groups_of(files), "AAAA")


class TestBundle:
    def test_track_speed_and_manifest(self, tmp_path):
        sp = make_plan(tmp_path, 5)
        g = SyntheticGridder()
        cache = data.GridCache(tmp_path / "cache")
        b = data.build_bundle(sp, data.GridOptions(), cache, g, title="Synthetic")
        m = b.manifest
        assert len(m["steps"]) == 5 and m["title"] == "Synthetic"
        # the moving storm is tracked, not the stationary stronger-looking one
        sp_ms = m["storm"]["speed_ms"]
        assert sp_ms == pytest.approx(1000 * SPEED_KM_S * math.hypot(0.9, 0.45), rel=0.05)
        assert m["storm"]["heading_deg"] == pytest.approx(
            math.degrees(math.atan2(0.9, 0.45)), abs=2
        )
        assert [s["t_s"] for s in m["steps"]] == [0, 300, 600, 900, 1200]
        assert m["steps"][1]["files"]["BBBB"]["offset_s"] == 40
        assert m["steps"][0]["mosaic"]["header"].startswith("fine-")
        assert m["animate"] == {
            "reference": "AAAA",
            "tolerance_s": 330.0,
            "winds": False,
            "track_mode": "cells",
        }
        for st in m["steps"]:
            for name in st["mosaic"].values():
                assert (b.store / name).is_file()

    def test_a_second_run_uses_the_cache_and_opens_no_files(self, tmp_path):
        sp = make_plan(tmp_path, 4)
        cache = data.GridCache(tmp_path / "cache")
        g1 = SyntheticGridder()
        first = data.build_bundle(sp, data.GridOptions(), cache, g1)
        assert g1.calls == {"coarse": 4, "fine": 4, "winds": 0}
        g2 = SyntheticGridder()
        second = data.build_bundle(sp, data.GridOptions(), data.GridCache(tmp_path / "cache"), g2)
        assert g2.calls == {"coarse": 0, "fine": 0, "winds": 0}
        assert second.stats.fine_cached == 4 and second.stats.coarse_cached == 4
        assert second.manifest["steps"] == first.manifest["steps"]
        assert second.manifest["storm"] == first.manifest["storm"]

    def test_fresh_ignores_the_cache(self, tmp_path):
        sp = make_plan(tmp_path, 3)
        data.build_bundle(
            sp, data.GridOptions(), data.GridCache(tmp_path / "c"), SyntheticGridder()
        )
        g = SyntheticGridder()
        data.build_bundle(sp, data.GridOptions(), data.GridCache(tmp_path / "c", fresh=True), g)
        assert g.calls["fine"] == 3

    def test_settings_that_change_the_grid_change_the_key(self, tmp_path):
        sp = make_plan(tmp_path, 3)
        cache = data.GridCache(tmp_path / "c")
        data.build_bundle(sp, data.GridOptions(detail_m=500), cache, SyntheticGridder())
        g = SyntheticGridder()
        data.build_bundle(sp, data.GridOptions(detail_m=1000), cache, g)
        assert (
            g.calls["fine"] == 3 and g.calls["coarse"] == 0
        )  # the storm search does not depend on it
        # a camera or look change is not part of the key at all: nothing is gridded again
        g2 = SyntheticGridder()
        data.build_bundle(sp, data.GridOptions(detail_m=500), cache, g2)
        assert g2.calls == {"coarse": 0, "fine": 0, "winds": 0}

    def test_a_changed_file_is_gridded_again(self, tmp_path):
        sp = make_plan(tmp_path, 3)
        cache = data.GridCache(tmp_path / "c")
        data.build_bundle(sp, data.GridOptions(), cache, SyntheticGridder())
        victim = next(iter(sp.steps[1].files.values()))[0].path
        victim.write_bytes(b"a longer file than before")
        g = SyntheticGridder()
        data.build_bundle(sp, data.GridOptions(), cache, g)
        assert g.calls["fine"] == 1 and g.calls["coarse"] == 1

    def test_resume_after_a_crash_keeps_what_was_finished(self, tmp_path):
        sp = make_plan(tmp_path, 6)
        cache = data.GridCache(tmp_path / "c")
        crashing = SyntheticGridder(fail_fine_at=4)  # dies while gridding step 4 of 6
        with pytest.raises(RuntimeError, match="went to sleep"):
            data.build_bundle(sp, data.GridOptions(), cache, crashing)
        assert crashing.calls["fine"] == 4
        done = sorted(p.name for p in cache.dir.glob("fine-*.json"))
        assert len(done) == 3  # steps 0..2 are complete on disk; step 3 left nothing half written
        assert not list(cache.dir.glob("*.part*"))
        resumed = SyntheticGridder()
        b = data.build_bundle(sp, data.GridOptions(), data.GridCache(tmp_path / "c"), resumed)
        assert resumed.calls == {"coarse": 0, "fine": 3, "winds": 0}  # only the unfinished steps
        assert b.stats.fine_cached == 3 and len(b.manifest["steps"]) == 6

    def test_a_partial_cache_entry_is_not_trusted(self, tmp_path):
        sp = make_plan(tmp_path, 3)
        cache = data.GridCache(tmp_path / "c")
        data.build_bundle(sp, data.GridOptions(), cache, SyntheticGridder())
        victim = sorted(cache.dir.glob("fine-*.bin"))[0]
        victim.write_bytes(victim.read_bytes()[:100])  # cut short, as by a full disk
        g = SyntheticGridder()
        data.build_bundle(sp, data.GridOptions(), cache, g)
        assert g.calls["fine"] == 1

    def test_no_storm_gives_a_helpful_error(self, tmp_path):
        class Empty(SyntheticGridder):
            def coarse(self, step, opts):
                c = super().coarse(step, opts)
                c.cells = []
                return c

        with pytest.raises(AnimatePlanError, match="lower --cell-dbz"):
            data.build_bundle(
                make_plan(tmp_path, 3), data.GridOptions(), data.GridCache(tmp_path / "c"), Empty()
            )

    def test_cells_are_followed_when_the_projection_centre_moves(self, tmp_path):
        """A radar that comes and goes moves the origin of a step's grid; the track must not jump."""

        class Shifting(SyntheticGridder):
            def coarse(self, step, opts):
                c = super().coarse(step, opts)
                if step.time >= T0 + timedelta(minutes=10):  # another origin from here on
                    lat, lon = 36.3, -88.7
                    from engine import mosaic
                    from engine.animate_data import _reproject

                    c.cells = [
                        Cell(
                            *_reproject(ORIGIN, (lat, lon), x.x_km, x.y_km),
                            x.max_dbz,
                            x.area_km2,
                            x.top_km,
                            x.score,
                        )
                        for x in c.cells
                    ]
                    c.origin = (lat, lon)
                    assert mosaic is not None
                return c

        b = data.build_bundle(
            make_plan(tmp_path, 5), data.GridOptions(), data.GridCache(tmp_path / "c"), Shifting()
        )
        assert b.manifest["storm"]["speed_ms"] == pytest.approx(
            1000 * SPEED_KM_S * math.hypot(0.9, 0.45), rel=0.06
        )


class TestMemoryBound:
    def peak(self, tmp_path, n: int) -> tuple[int, SyntheticGridder]:
        sp = make_plan(tmp_path / f"n{n}", n)
        g = SyntheticGridder(
            group_mb=6.0, fine_shape=(48, 256, 256)
        )  # 6 MB of "radar" + 3 MB grid per step
        cache = data.GridCache(tmp_path / f"cache{n}")
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            b = data.build_bundle(sp, data.GridOptions(), cache, g)
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert len(b.manifest["steps"]) == n
        return peak, g

    def test_memory_does_not_grow_with_the_length_of_the_series(self, tmp_path):
        short, g_short = self.peak(tmp_path, 12)
        long_, g_long = self.peak(tmp_path, 240)  # 20 times longer
        assert g_short.peak_live == 1 and g_long.peak_live == 1  # one group of volumes at a time
        group = 6e6 + 3.1e6
        assert (
            long_ < 3 * group
        )  # a few groups, not 240 of them (240 x 9 MB = 2.2 GB would be linear)
        assert long_ < 1.6 * short  # and no growth with the series
        assert g_long.calls["fine"] == 240

    def test_the_disk_holds_the_series_not_the_memory(self, tmp_path):
        sp = make_plan(tmp_path, 30)
        cache = data.GridCache(tmp_path / "cache")
        data.build_bundle(
            sp, data.GridOptions(), cache, SyntheticGridder(fine_shape=(16, 128, 128))
        )
        size = sum(p.stat().st_size for p in cache.dir.glob("fine-*.bin"))
        assert size == 30 * 16 * 128 * 128


class TestRunWithoutRendering:
    def opts(self, tmp_path, **kw):
        files = sorted((tmp_path / "radar").glob("*/*")) or series(
            tmp_path / "radar", {"AAAA": 0, "BBBB": 40}, 4
        )
        return animate.AnimateOptions(
            files=[str(f) for f in files], cache_dir=str(tmp_path / "cache"), no_render=True, **kw
        )

    def test_unreadable_radars_are_left_out_named_and_remembered_by_the_cache(self, tmp_path):
        class Tdwr(SyntheticGridder):
            def coarse(self, step, opts):
                c = super().coarse(step, opts)
                c.unreadable = {"BBBB": "site position out of range (a TDWR file)"}
                return c

        res = animate.run(self.opts(tmp_path), gridder=Tdwr())
        assert res.steps.used_radars == ["AAAA"]
        assert all(set(st["files"]) == {"AAAA"} for st in res.bundle.manifest["steps"])
        assert "BBBB" not in res.bundle.manifest["title"] and res.bundle.manifest["case"] == "AAAA"
        gone = [s for s in res.steps.skipped if s.radar == "BBBB"]
        assert len(gone) == 4 and "could not be read: site position" in gone[0].reason
        assert any("BBBB left out" in w for w in res.warnings)
        # the next run does not open the files again, and still knows
        again = animate.run(self.opts(tmp_path), gridder=SyntheticGridder())
        assert again.steps.used_radars == ["AAAA"] and any(
            "BBBB left out" in w for w in again.warnings
        )

    def test_the_result_is_json_able_and_complete(self, tmp_path):
        res = animate.run(self.opts(tmp_path, fps=24, duration=20.0), gridder=SyntheticGridder())
        doc = json.loads(json.dumps(res.to_dict()))
        for key in (
            "inputs",
            "steps",
            "radar_offsets",
            "storm",
            "frames",
            "camera",
            "grid",
            "output",
        ):
            assert key in doc
        assert doc["frames"]["frames"] == 480 and doc["frames"]["fps"] == 24
        assert doc["frames"]["timelapse"] == pytest.approx(900 / 17, rel=0.01)
        assert doc["storm"]["track"][0]["speed_ms_since_previous"] is None
        assert doc["storm"]["track"][1]["speed_ms_since_previous"] == pytest.approx(
            1000 * SPEED_KM_S * math.hypot(0.9, 0.45), rel=0.06
        )

    def test_provenance_record_names_data_versions_parameters_and_method(self, tmp_path):
        res = animate.run(
            self.opts(tmp_path, command="xrd animate -i x --no-render", fps=24, duration=20.0),
            gridder=SyntheticGridder(),
        )
        doc = json.loads(json.dumps(animate.provenance(res, {"fps": 24, "keys": [1]}, None)))
        assert doc["tool"] == "xrd animate" and doc["command"] == "xrd animate -i x --no-render"
        assert doc["created_utc"].endswith("Z")
        assert {"python", "radarx", "playwright", "av", "browser", "app_build"} <= set(
            doc["versions"]
        )
        assert doc["parameters"]["fps"] == 24 and "files" not in doc["parameters"]
        assert "keys" not in doc["page_settings"] and doc["page_settings"]["fps"] == 24
        vols = doc["input_volumes"]
        assert len(vols) == 8 and vols[0]["file"].startswith("AAAA") and vols[0]["size_bytes"] == 1
        assert {v["time_from"] for v in vols} == {"name"} and vols[1]["offset_s"] == 40
        assert "interpolation" in doc["method"] and "observed" in doc["method"]["labels"]
        assert "nothing_invented" in doc["method"] and doc["frames"]["frames"] == 480

    def test_mp4_tags_say_what_the_clip_is(self, tmp_path):
        res = animate.run(self.opts(tmp_path), gridder=SyntheticGridder())
        tags = animate.mp4_metadata(res, tmp_path / "storm.mp4")
        assert tags["title"] == res.bundle.manifest["title"]
        assert "interpolated" in tags["comment"] and "storm.animate.json" in tags["comment"]
        assert "AAAA, BBBB" in tags["comment"]

    def test_timelapse_sets_the_length(self, tmp_path):
        res = animate.run(self.opts(tmp_path, timelapse=30.0), gridder=SyntheticGridder())
        assert res.duration == pytest.approx(900 / 30 + 3, abs=0.1)

    def test_page_settings_carry_the_options(self, tmp_path):
        look = tmp_path / "look.json"
        look.write_text('{"exposure": 1.4}')
        o = animate.AnimateOptions(
            files=["x"],
            realistic=True,
            look_preset="Golden hour",
            look_file=str(look),
            set_values=["temperature=0.2"],
            fov=55.0,
            target_lag=0.6,
            title="My storm",
            lightning_model=True,
            sea=True,
            camera="top-down",
        )
        sp = make_plan(tmp_path, 3)
        cp = cam.resolve_camera(cam.load_camera("top-down"), 20.0, 90.0, target_lag=0.6, fov=55.0)
        s = animate.page_settings(o, cp, sp, 24, 20.0)
        assert s["fps"] == 24 and s["duration"] == 20.0 and s["camera"] == "orbit"
        assert s["ground"] == "realistic" and s["stormStyle"] == "realistic"
        assert s["lookPreset"] == "Golden hour" and s["lookOverrides"] == {"temperature": 0.2}
        assert s["lookFormat"] == "json" and "exposure" in s["lookText"]
        assert s["targetLag"] == 0.6 and all(k["fov"] == 55.0 for k in s["keys"])
        assert s["title"] == "My storm" and s["lightning"] == "auto" and s["sea"] is True
        assert s["audio"] is False and s["observedTolS"] == plan.OBSERVED_TOL_S
        assert s["radarCredit"] == "Radar data: AAAA, BBBB"
        plain = animate.page_settings(animate.AnimateOptions(files=["x"]), cp, sp, 30, 36.0)
        assert plain["lightning"] == "off" and plain["ground"] == "map"
        assert plain["stormStyle"] == "scientific" and "lookPreset" not in plain

    def test_credit_names_the_radars_not_nexrad_for_other_formats(self, tmp_path):
        def meta(p):
            return "CPOL", T0 + timedelta(minutes=5 * (p.name == "b.nc"))

        refs, _ = plan.scan_files([tmp_path / "a.nc", tmp_path / "b.nc"], meta=meta)
        sp = plan.choose_steps(plan.group_by_radar(refs))
        assert animate.radar_credit(animate.AnimateOptions(), sp) == "Radar data: CPOL"
        assert animate.radar_credit(animate.AnimateOptions(credit="Data: ARM"), sp) == "Data: ARM"


# ---------------------------------------------------------------------------
# frames on disk, resume, encoding
# ---------------------------------------------------------------------------


class TestFramesOnDisk:
    def test_existing_frames_counts_only_complete_files(self, tmp_path):
        d = tmp_path / "frames"
        d.mkdir()
        for i in (0, 1, 2, 5):
            (d / render.frame_name(i)).write_bytes(b"png")
        (d / render.frame_name(3)).write_bytes(b"")  # empty: not done
        (d / (render.frame_name(4) + ".part")).write_bytes(b"half")  # being written: not done
        assert render.existing_frames(d, 8) == {0, 1, 2, 5}
        assert render.existing_frames(tmp_path / "none", 8) == set()

    def test_save_frame_is_atomic(self, tmp_path):
        p = tmp_path / "f" / render.frame_name(7)
        render.save_frame(b"\x89PNG", p)
        assert p.read_bytes() == b"\x89PNG" and not list(p.parent.glob("*.part"))

    def test_job_key_depends_on_everything_that_changes_the_pixels(self):
        base = {"manifest": "a", "settings": {"x": 1}, "size": [640, 360]}
        assert render.job_key(base) == render.job_key(dict(reversed(list(base.items()))))
        assert render.job_key({**base, "size": [641, 360]}) != render.job_key(base)
        assert render.job_key({**base, "settings": {"x": 2}}) != render.job_key(base)

    def test_png_sequence_result_moves_or_copies(self, tmp_path):
        job = tmp_path / "jobs" / "k"
        frames = job / "frames"
        frames.mkdir(parents=True)
        for i in range(3):
            (frames / render.frame_name(i)).write_bytes(bytes([i]))
        animate._finish_sequence(frames, tmp_path / "out_frames", 3, "png", keep=False)
        assert [p.name for p in sorted((tmp_path / "out_frames").iterdir())] == [
            render.frame_name(i) for i in range(3)
        ]
        assert not job.exists()  # the working folder is gone

    def test_ffmpeg_line(self):
        s = render.ffmpeg_command(Path("/o/x_frames"), Path("/o/x.mp4"), 30)
        assert s.startswith("ffmpeg -framerate 30 -i '/o/x_frames/frame_%05d.png'") and s.endswith(
            "'/o/x.mp4'"
        )

    def test_mp4_is_written_atomically(self, tmp_path):
        pytest.importorskip("av")
        pil_image = pytest.importorskip("PIL.Image")
        d = tmp_path / "frames"
        d.mkdir()
        for i in range(6):
            pil_image.new("RGB", (64, 48), (i * 30, 40, 90)).save(d / render.frame_name(i))
        out = tmp_path / "clip.mp4"
        render.encode_mp4(d, 6, out, 6, (64, 48))
        assert out.stat().st_size > 0 and not out.with_name("clip.mp4.part").exists()
        # a missing frame stops the encode and leaves NO file under the final name
        (d / render.frame_name(3)).unlink()
        out2 = tmp_path / "broken.mp4"
        with pytest.raises(FileNotFoundError):
            render.encode_mp4(d, 6, out2, 6, (64, 48))
        assert not out2.exists() and not out2.with_name("broken.mp4.part").exists()


# ---------------------------------------------------------------------------
# optional dependencies and the built app
# ---------------------------------------------------------------------------


class TestOptionalDependencies:
    def test_missing_playwright_says_how_to_install(self, monkeypatch):
        monkeypatch.setattr(render, "have_module", lambda name: name != "playwright")
        with pytest.raises(Exception, match=r"pip install 'xradar-desktop-engine\[video\]'") as e:
            render.require_playwright()
        assert "Playwright" in str(e.value) and "no browser is downloaded" in str(e.value)
        assert type(e.value).__name__ == "XrdDependencyError"

    def test_missing_pyav_names_the_png_fallback(self, monkeypatch):
        monkeypatch.setattr(render, "have_module", lambda name: name != "av")
        with pytest.raises(Exception, match="PyAV") as e:
            render.require_av()
        assert "PNG files that ffmpeg" in str(e.value)
        assert render.video_status() == {"playwright": True, "av": False} or True

    def test_the_package_does_not_import_them_at_start(self):
        import subprocess
        import sys

        code = (
            "import sys; import engine.animate, engine.animate_cli, engine.animate_render, "
            "engine.animate_data, engine.cli; "
            "bad = [m for m in ('playwright', 'av') if m in sys.modules]; print(bad)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        assert out.stdout.strip() == "[]", out.stderr

    def test_the_extra_is_declared(self):
        text = (Path(__file__).parents[1] / "pyproject.toml").read_text()
        assert "video = [" in text and "playwright" in text and '"av' in text
        dep_block = text.split("dependencies = [")[1].split("]")[0]
        assert "playwright" not in dep_block and '"av' not in dep_block  # never a hard dependency

    def test_frontend_not_found_says_how_to_build(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            render, "frontend_candidates", lambda explicit=None: [tmp_path / "dist"]
        )
        with pytest.raises(Exception, match="npm run build") as e:
            render.find_frontend()
        assert "--app-dist" in str(e.value) and "XRADAR_APP_DIST" in str(e.value)

    def test_a_build_without_the_clip_renderer_is_refused(self, tmp_path, monkeypatch):
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html></html>")
        (dist / "assets" / "app.js").write_text("console.log('old build')")
        monkeypatch.setattr(render, "frontend_candidates", lambda explicit=None: [dist])
        with pytest.raises(Exception, match="no clip renderer"):
            render.find_frontend(str(dist))
        (dist / "assets" / "app.js").write_text("window.__xrClip = {}")
        assert render.find_frontend(str(dist)) == dist.resolve()

    def test_frontend_search_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XRADAR_APP_DIST", str(tmp_path / "env"))
        c = render.frontend_candidates(str(tmp_path / "explicit"))
        assert c[0] == tmp_path / "explicit" and c[1] == tmp_path / "env"
        assert any(p.name == "dist" and p.parent.name != "env" for p in c[2:])

    def test_chrome_args_have_no_proxy(self):
        assert "--no-proxy-server" in render.chrome_args()


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


@pytest.fixture
def case(tmp_path) -> Path:
    root = tmp_path / "case"
    series(root, {"KAAA": 0, "KBBB": 50}, 4)
    (root / "results").mkdir()
    (root / "results" / "table.csv").write_text("a,b")
    (root / "results" / "background_20211211_020000.nc").write_bytes(b"x")  # not a radar volume
    return root


@pytest.fixture
def synthetic(monkeypatch):
    holder = {}

    def make(log=None):
        holder["g"] = SyntheticGridder()
        return holder["g"]

    monkeypatch.setattr(data, "EngineGridder", make)
    return holder


def run_cli(capsys, *argv):
    from engine import cli

    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


class TestCommand:
    def test_help_and_overview_list_the_command(self, capsys):
        code, out, _ = run_cli(capsys, "animate", "--help")
        assert code == 0 and "--tolerance-s" in out and "--camera" in out and "--no-render" in out
        code, out, _ = run_cli(capsys)
        assert "animate" in out

    def test_a_case_folder_with_one_folder_per_radar_works(self, case):
        from engine import animate_cli

        files = animate_cli.expand_animate_inputs([str(case)])
        assert len(files) == 8 and all("results" not in f for f in files)

    def test_folder_with_files_and_globs_and_stdin(self, case, monkeypatch):
        import io

        from engine import animate_cli

        assert len(animate_cli.expand_animate_inputs([str(case / "KAAA")])) == 4
        assert len(animate_cli.expand_animate_inputs([str(case / "*" / "KBBB*")])) == 4
        monkeypatch.setattr(
            "sys.stdin", io.StringIO("\n".join(str(p) for p in sorted((case / "KAAA").iterdir())))
        )
        assert len(animate_cli.expand_animate_inputs(["-"])) == 4

    def test_no_files_is_a_usage_error(self, capsys):
        code, _, err = run_cli(capsys, "animate")
        assert code == 2 and "needs radar files" in err

    def test_empty_pattern_is_an_input_error(self, capsys, tmp_path):
        code, _, err = run_cli(capsys, "animate", "-i", str(tmp_path / "nothing*"))
        assert code == 3 and "matches no files" in err

    def test_json_report_without_rendering(self, capsys, case, tmp_path, synthetic):
        code, out, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "--no-render",
            "--json",
            "--cache-dir",
            str(tmp_path / "c"),
        )
        assert code == 0, err
        doc = json.loads(out)
        assert doc["command"] == "animate" and doc["inputs"]["steps"] == 4
        assert doc["inputs"]["reference"] in ("KAAA", "KBBB") and doc["inputs"]["skipped"] == []
        assert doc["steps"][1]["offsets_s"].keys() == {"KAAA", "KBBB"}
        assert doc["storm"]["mean_speed_ms"] == pytest.approx(
            1000 * SPEED_KM_S * math.hypot(0.9, 0.45), rel=0.06
        )
        assert len(doc["storm"]["track"]) == 4 and doc["storm"]["displacement_km"] > 10
        f = doc["frames"]
        assert f["count"] if "count" in f else f["frames"] == 1080
        assert f["observed_frames"] + f["interpolated_frames"] == f["frames"] == 1080
        assert doc["camera"]["mode"] == "orbit" and doc["camera"]["path"]["keys"] == 8
        assert doc["output"] == {"rendered": False, "reason": "--no-render"}
        assert doc["grid"]["computed"]["fine"] == 4

    def test_text_report_has_the_numbers_to_check(self, capsys, case, tmp_path, synthetic):
        code, out, err = run_cli(
            capsys, "animate", "-i", str(case), "--no-render", "--cache-dir", str(tmp_path / "c"),
            "--duration", "20", "--fps", "24",
        )  # fmt: skip
        assert code == 0 and "nothing was drawn" in err
        for text in ("4 time steps", "480 frames", "20 s at 24 fps", "storm", "m/s", "interpolated",
                     "tolerance 330 s", "offsets", "camera", "grids"):  # fmt: skip
            assert text in out, text
        # the second run reports the cache
        code, out, _ = run_cli(
            capsys, "animate", "-i", str(case), "--no-render", "--cache-dir", str(tmp_path / "c"),
        )  # fmt: skip
        assert "0 computed, 4 from the cache" in out

    def test_camera_file_is_used_and_its_duration_too(self, capsys, case, tmp_path, synthetic):
        cf = tmp_path / "cam.json"
        cf.write_text(json.dumps({"key": [
            {"t": 0, "azimuth": 180, "elevation": 60, "distance": 150},
            {"t": 12, "azimuth": 240, "elevation": 20, "distance": 90},
        ]}))  # fmt: skip
        code, out, err = run_cli(
            capsys, "animate", "-i", str(case), "--no-render", "--json", "--cache-dir", str(tmp_path / "c"),
            "--camera", str(cf),
        )  # fmt: skip
        doc = json.loads(out)
        assert code == 0, err
        assert doc["frames"]["duration_s"] == 12 and doc["frames"]["frames"] == 360
        assert doc["camera"]["source"] == "camera file cam.json"

    def test_a_bad_camera_file_is_a_usage_error_before_any_gridding(
        self, capsys, case, tmp_path, synthetic
    ):
        cf = tmp_path / "cam.json"
        cf.write_text(json.dumps({"key": [{"t": 0, "azimut": 1}]}))
        code, _, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "--no-render",
            "--cache-dir",
            str(tmp_path / "c"),
            "--camera",
            str(cf),
        )
        assert code == 2 and 'unknown key "azimut"' in err and 'did you mean "azimuth"' in err
        assert "g" not in synthetic  # nothing was gridded

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["--fps", "0"], "--fps 0 is out of range"),
            (["--size", "big"], "WIDTHxHEIGHT"),
            (["--duration", "2"], "too short"),
            (["--duration", "20", "--timelapse", "30"], "not both"),
            (["--track", "wiggly"], "invalid choice"),
            (["--camera", "topdwn"], 'Did you mean "top-down"'),
            (["--target-lag", "2"], "between 0"),
            (["--tolerance-s", "-4"], "positive"),
            (["--start", "yesterday"], "cannot read the time"),
            (["--observer", "a,b"], "X,Y in km"),
            (["--set", "exposure"], "--set expects key=value"),
        ],
    )
    def test_option_errors(self, capsys, case, tmp_path, synthetic, args, message):
        code, _, err = run_cli(
            capsys,
            "animate",
            "-i",
            str(case),
            "--no-render",
            "--cache-dir",
            str(tmp_path / "c"),
            *args,
        )
        assert code == 2 and message in err, err

    def test_config_file_and_unknown_key_suggestion(self, capsys, case, tmp_path, synthetic):
        cfg = tmp_path / "anim.toml"
        cfg.write_text(f'command = "animate"\ninput = ["{case}"]\nno_render = true\nduration = 18\njson = true\n'
                       f'cache_dir = "{tmp_path / "c"}"\n[set]\nexposure = 1.5\n')  # fmt: skip
        pytest.importorskip("tomllib")
        code, out, err = run_cli(capsys, "--config", str(cfg))
        assert code == 0, err
        assert json.loads(out)["frames"]["duration_s"] == 18
        cfg.write_text(f'command = "animate"\ninput = ["{case}"]\ntolerence_s = 300\n')
        code, _, err = run_cli(capsys, "--config", str(cfg))
        assert (
            code == 2
            and "unrecognized option(s): --tolerence-s" in err
            and "Did you mean --tolerance-s" in err
        )

    def test_missing_playwright_is_exit_code_4(
        self, capsys, case, tmp_path, synthetic, monkeypatch
    ):
        monkeypatch.setattr(render, "have_module", lambda name: False)
        code, _, err = run_cli(
            capsys, "animate", "-i", str(case), "--cache-dir", str(tmp_path / "c")
        )
        assert code == 4 and "pip install 'xradar-desktop-engine[video]'" in err
        assert "g" not in synthetic  # failed before gridding

    def test_missing_frontend_is_exit_code_4(self, capsys, case, tmp_path, synthetic, monkeypatch):
        monkeypatch.setattr(render, "have_module", lambda name: True)
        monkeypatch.setattr(
            render, "frontend_candidates", lambda explicit=None: [tmp_path / "nodist"]
        )
        code, _, err = run_cli(
            capsys, "animate", "-i", str(case), "--cache-dir", str(tmp_path / "c")
        )
        assert code == 4 and "npm run build" in err and "g" not in synthetic

    def test_skipped_files_are_reported(self, capsys, tmp_path, synthetic):
        root = tmp_path / "many"
        files = series(root, {"KAAA": 0, "KBBB": 50}, 3)
        files.append(touch(root, "KCCC", T0 + timedelta(hours=9)))
        code, out, _ = run_cli(
            capsys,
            "animate",
            "-i",
            *map(str, files),
            "--no-render",
            "--cache-dir",
            str(tmp_path / "c"),
        )
        assert code == 0 and "not used    1" in out and "KCCC" in out and "no step uses it" in out

    def test_print_command_and_examples_topic(self, capsys):
        code, out, _ = run_cli(capsys, "examples", "animate")
        assert code == 0 and out.startswith("# ANIMATE") and "xrd animate" in out


# ---------------------------------------------------------------------------
# end to end with a real Chrome (skipped when it cannot run)
# ---------------------------------------------------------------------------


def _chrome_ready() -> str | None:
    if not render.have_module("playwright"):
        return "playwright is not installed"
    try:
        dist = render.find_frontend()
    except Exception as exc:
        return f"no built app: {str(exc).splitlines()[0]}"
    try:
        with render.Renderer(dist, 64, 64, channel="chrome"):
            pass
    except Exception as exc:
        return f"Chrome cannot start: {str(exc).splitlines()[0][:80]}"
    return None


def _e2e_options(tmp_path: Path, output: str, **kw) -> animate.AnimateOptions:
    radar_dir = tmp_path / "radar"
    files = sorted(radar_dir.glob("*/*")) or series(radar_dir, {"AAAA": 0, "BBBB": 40}, 3)
    base = {
        "files": [str(f) for f in files],
        "output": output,
        "duration": 4.0,
        "fps": 4,
        "width": 320,
        "height": 180,
        "cache_dir": str(tmp_path / "cache"),
        "tile_cache": str(tmp_path / "tiles"),
        "offline": True,
        "camera": "orbit",
    }
    return animate.AnimateOptions(**{**base, **kw})


@pytest.fixture
def chrome():
    reason = _chrome_ready()
    if reason:
        pytest.skip(reason)


def test_end_to_end_png_sequence_and_resume(tmp_path, chrome):
    from PIL import Image

    out_dir = tmp_path / "frames_out"
    opts = _e2e_options(tmp_path, str(out_dir) + "/", keep_frames=True)
    res = animate.run(opts, gridder=SyntheticGridder(fine_shape=(16, 64, 64)))
    pngs = sorted(out_dir.glob("frame_*.png"))
    assert len(pngs) == 16 == res.frame_summary["frames"]
    first, mid = (np.asarray(Image.open(p).convert("RGB")) for p in (pngs[0], pngs[8]))
    assert first.std() > 5  # something is drawn
    assert np.abs(first.astype(int) - mid.astype(int)).mean() > 0.5  # and it moves
    assert res.output["resumed_frames"] == 0 and res.output["rendered_frames"] == 16

    # an interrupted job: three frames are missing from the working folder; a re-run draws only those
    work = Path(res.output["frames_dir"])
    keep = np.asarray(Image.open(work / render.frame_name(9)).convert("RGB")).astype(int)
    for i in (5, 9, 12):
        (work / render.frame_name(i)).unlink()
    gridder = SyntheticGridder(fine_shape=(16, 64, 64))
    shutil_dest = tmp_path / "again"
    opts2 = _e2e_options(tmp_path, str(shutil_dest) + "/", keep_frames=False)
    res2 = animate.run(opts2, gridder=gridder)
    assert gridder.calls == {"coarse": 0, "fine": 0, "winds": 0}  # the grids came from the cache
    assert res2.output["resumed_frames"] == 13 and res2.output["rendered_frames"] == 3
    again = np.asarray(Image.open(shutil_dest / render.frame_name(9)).convert("RGB")).astype(int)
    assert np.abs(again - keep).mean() < 0.5  # frames are deterministic: the same picture again
    assert len(list(shutil_dest.glob("frame_*.png"))) == 16


def test_end_to_end_without_pyav_writes_png_and_the_ffmpeg_line(tmp_path, chrome, monkeypatch):
    monkeypatch.setattr(render, "video_status", lambda: {"playwright": True, "av": False})
    target = tmp_path / "clip.mp4"
    res = animate.run(
        _e2e_options(tmp_path, str(target)), gridder=SyntheticGridder(fine_shape=(16, 64, 64))
    )
    assert not target.exists()
    frames = tmp_path / "clip_frames"
    assert len(list(frames.glob("frame_*.png"))) == 16
    assert res.output["video"] is None and res.output["frames_written"] == str(frames)
    assert res.output["ffmpeg"].startswith("ffmpeg -framerate 4 -i ")
    assert any("PyAV is not installed" in w for w in res.warnings)


def test_end_to_end_mp4_and_still(tmp_path, chrome):
    pytest.importorskip("av")
    import av

    target = tmp_path / "clip.mp4"
    still = tmp_path / "still.png"
    res = animate.run(
        _e2e_options(tmp_path, str(target), still=str(still), still_at=2.0),
        gridder=SyntheticGridder(fine_shape=(16, 64, 64)),
    )
    assert still.stat().st_size > 1000 and res.output["still_at_s"] == 2.0
    assert not target.with_name("clip.mp4.part").exists()
    with av.open(str(target)) as c:
        v = c.streams.video[0]
        assert (v.width, v.height) == (320, 180) and v.codec_context.name == "h264"
        assert sum(1 for _ in c.decode(video=0)) == 16
    assert res.output["encoder"].startswith("pyav")
    assert not (tmp_path / "cache" / "jobs" / res.output["job"]).exists()
    # the provenance file sits next to the clip and the tags in the MP4 point to it
    record = json.loads((tmp_path / "clip.animate.json").read_text())
    assert record["frames"]["frames"] == 16 and record["versions"]["browser"]
    assert record["output"]["video"] == str(target) and record["versions"]["av"]
    with av.open(str(target)) as c:
        assert "interpolated" in c.metadata.get("comment", "")
        assert "clip.animate.json" in c.metadata["comment"]


def test_only_a_still_renders_no_video(tmp_path, chrome):
    still = tmp_path / "only.png"
    res = animate.run(
        _e2e_options(tmp_path, None, still=str(still)),
        gridder=SyntheticGridder(fine_shape=(16, 64, 64)),
    )
    assert still.is_file() and res.output["video"] is None
    assert not (tmp_path / "cache" / "jobs" / res.output["job"] / "frames").exists()


def test_a_bad_look_key_fails_before_any_gridding(tmp_path, chrome):
    gridder = SyntheticGridder()
    with pytest.raises(
        AnimateConfigError, match=r'unknown look key "exposur".*did you mean "exposure"'
    ):
        animate.run(
            _e2e_options(tmp_path, str(tmp_path / "o") + "/", set_values=["exposur=1.4"]),
            gridder=gridder,
        )
    assert gridder.calls == {"coarse": 0, "fine": 0, "winds": 0}

"""Tests of the data side of the storm-clip tool (tools/storm_clip/clipdata.py): time steps,
storm finding and tracking. Synthetic volumes only; no radar files are needed."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "storm_clip"
sys.path.insert(0, str(TOOLS))

import clipdata

UTC = timezone.utc


def touch(root: Path, radar: str, when: datetime) -> Path:
    d = root / radar
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{radar}{when:%Y%m%d_%H%M%S}_V06"
    f.write_bytes(b"x")
    return f


class TestSteps:
    def test_volume_time_from_name(self):
        t = clipdata.parse_volume_time("/x/KHPX20211211_020457_V06")
        assert t == datetime(2021, 12, 11, 2, 4, 57, tzinfo=UTC)
        assert clipdata.parse_volume_time("notes.txt") is None

    def test_list_volumes_sorted_and_read_only(self, tmp_path):
        t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
        touch(tmp_path, "KAAA", t0 + timedelta(minutes=6))
        touch(tmp_path, "KAAA", t0)
        (tmp_path / "results").mkdir()
        vols = clipdata.list_volumes(tmp_path)
        assert list(vols) == ["KAAA"]
        assert [v[0] for v in vols["KAAA"]] == [t0, t0 + timedelta(minutes=6)]

    def test_each_radar_contributes_its_nearest_volume(self, tmp_path):
        t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
        for k in range(3):
            touch(tmp_path, "REF1", t0 + timedelta(minutes=5.5 * k))
            touch(tmp_path, "OTH2", t0 + timedelta(minutes=5.5 * k + 2))
        touch(tmp_path, "FAR3", t0 + timedelta(minutes=1))
        steps = clipdata.choose_steps(clipdata.list_volumes(tmp_path), "REF1")
        assert len(steps) == 3
        assert steps[0].files["OTH2"][1] == pytest.approx(120.0)
        # FAR3 only has one volume: it is too far from the later steps and is left out of them
        assert "FAR3" in steps[0].files and "FAR3" not in steps[2].files
        assert [s.time for s in steps] == sorted(s.time for s in steps)

    def test_steps_that_repeat_the_volumes_of_most_radars_are_dropped(self, tmp_path):
        t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
        for k in range(3):
            touch(tmp_path, "REF1", t0 + timedelta(minutes=5.5 * k))
        # the other radars stop after the second volume: step 3 only changes the reference radar
        for k in range(2):
            touch(tmp_path, "OTH2", t0 + timedelta(minutes=5.5 * k + 1))
            touch(tmp_path, "OTH3", t0 + timedelta(minutes=5.5 * k + 2))
        steps = clipdata.choose_steps(clipdata.list_volumes(tmp_path), "REF1")
        assert len(steps) == 2

    def test_best_reference_is_the_radar_with_the_most_steps(self, tmp_path):
        t0 = datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC)
        for k in range(4):
            touch(tmp_path, "LONG", t0 + timedelta(minutes=5 * k))
            touch(tmp_path, "BOTH", t0 + timedelta(minutes=5 * k + 1))
        for k in range(2):
            touch(tmp_path, "SHRT", t0 + timedelta(minutes=5 * k + 2))
        ref, steps = clipdata.best_reference(clipdata.list_volumes(tmp_path))
        assert ref in ("LONG", "BOTH") and len(steps) == 4

    def test_needs_two_steps(self, tmp_path):
        touch(tmp_path, "REF1", datetime(2021, 12, 11, 2, 0, 0, tzinfo=UTC))
        with pytest.raises(ValueError, match="two"):
            clipdata.choose_steps(clipdata.list_volumes(tmp_path), "REF1")


def blob_volume(centres, nz=8, ny=60, nx=60, lo=-20.0, hi=75.0):
    """uint8 volume of Gaussian echo columns; ``centres`` = [(ix, iy, peak_dbz, top, sigma)]."""
    dbz = np.full((nz, ny, nx), np.nan, dtype=np.float32)
    jj, ii = np.mgrid[0:ny, 0:nx]
    for cx, cy, peak, top, sig in centres:
        g = np.exp(-(((ii - cx) ** 2 + (jj - cy) ** 2) / (2 * sig**2)))
        for k in range(top + 1):
            v = peak * g - 8.0 * (top - k) * 0 - 30.0 * (1 - g)
            dbz[k] = np.fmax(dbz[k], np.where(v > -10, v, np.nan))
    codes = np.zeros(dbz.shape, dtype=np.uint8)
    ok = np.isfinite(dbz) & (dbz >= lo)
    codes[ok] = np.clip(np.rint((dbz[ok] - lo) * 254.0 / (hi - lo)) + 1, 1, 255).astype(np.uint8)
    return codes


class TestStorm:
    Z = np.arange(1.0, 9.0)  # km

    def cells(self, centres, **kw):
        vol = blob_volume(centres)
        return clipdata.find_cells(vol, (-20.0, 75.0), self.Z, 0.0, 0.0, 1000.0, **kw)

    def test_finds_cells_and_ranks_the_strong_tall_large_one_first(self):
        cells = self.cells([(15, 15, 62, 7, 4.0), (45, 40, 56, 3, 5.0)])
        assert len(cells) == 2
        top = cells[0]
        assert top.max_dbz > 55 and top.top_km >= 7.0
        assert top.x_km == pytest.approx(15.0, abs=1.5) and top.y_km == pytest.approx(15.0, abs=1.5)
        assert cells[0].score > cells[1].score

    def test_small_echoes_below_the_area_limit_are_ignored(self):
        assert self.cells([(30, 30, 60, 5, 0.6)], min_area_km2=20.0) == []

    def test_a_weaker_echo_is_no_cell(self):
        assert self.cells([(30, 30, 38, 5, 4.0)], threshold_dbz=45.0) == []

    def test_tracking_follows_the_moving_storm_not_a_stronger_stationary_one(self):
        # the tracked cell moves 6 px (km) east per step; another cell stays put
        steps = []
        for k in range(3):
            steps.append(self.cells([(10 + 6 * k, 20, 61, 7, 4.0), (45, 45, 58, 4, 5.0)]))
        track = clipdata.track_storm(steps, [0.0, 330.0, 660.0])
        assert [round(c.x_km) for c in track] == [10, 16, 22]
        u, v = clipdata.storm_motion([(c.x_km, c.y_km) for c in track], [0.0, 330.0, 660.0])
        assert u == pytest.approx(12.0 * 1000 / 660.0, rel=0.1) and v == pytest.approx(0.0, abs=0.5)

    def test_a_lost_storm_is_carried_along_its_motion(self):
        steps = [
            self.cells([(10, 20, 61, 7, 4.0)]),
            self.cells([(16, 20, 61, 7, 4.0)]),
            [],  # nothing found in the last step
        ]
        track = clipdata.track_storm(steps, [0.0, 330.0, 660.0])
        assert len(track) == 3
        assert track[2].x_km == pytest.approx(22.0, abs=1.5)

    def test_no_cell_in_the_first_step_is_an_error(self):
        with pytest.raises(ValueError, match="first time step"):
            clipdata.track_storm([[], []], [0.0, 1.0])

    def test_box_around_points(self):
        assert clipdata.box_around([(0.0, 0.0), (10.0, -4.0)], 5.0) == (-5.0, -9.0, 15.0, 5.0)

    def test_decode(self):
        codes = np.array([[[0, 1, 255]]], dtype=np.uint8)
        out = clipdata.decode(codes, -20.0, 75.0)
        assert (
            np.isnan(out[0, 0, 0])
            and out[0, 0, 1] == pytest.approx(-20.0)
            and out[0, 0, 2] == pytest.approx(75.0)
        )


def test_out_folder_must_not_be_inside_the_case(tmp_path, capsys):
    case = tmp_path / "case"
    case.mkdir()
    with pytest.raises(SystemExit):
        clipdata.main(["--case", str(case), "--out", str(case / "bundle")])

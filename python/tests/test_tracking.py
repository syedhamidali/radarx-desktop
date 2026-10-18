"""Tests for engine.tracking — cell identification (incl. 0/360 seam) and tracking."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from engine.tracking import (
    CellInfo,
    CellTrack,
    _compute_motion,
    beam_height_km,
    cells_from_labels,
    extrapolate_cell,
    identify_cells,
    identify_cells_labelled,
    match_nearest,
    polar_to_latlon,
    track_cells,
)

N_AZ = 360
RANGE_M = 250.0 + 250.0 * np.arange(400)


def _field(blobs: list[tuple[float, float, float, float, float]], az=None) -> xr.DataArray:
    """Gaussian blobs: (centre_az_deg, centre_range_m, sigma_az_deg, sigma_rng_m, peak_dbz)."""
    az = np.arange(N_AZ) + 0.5 if az is None else az
    a, r = np.meshgrid(az, RANGE_M, indexing="ij")
    f = np.full(a.shape, 0.0)
    for caz, crng, saz, srng, peak in blobs:
        da = (a - caz + 180.0) % 360.0 - 180.0
        f = np.maximum(f, peak * np.exp(-((da / saz) ** 2 + ((r - crng) / srng) ** 2)))
    return xr.DataArray(
        f.astype(np.float32),
        dims=("azimuth", "range"),
        coords={"azimuth": az, "range": RANGE_M},
    )


class TestIdentifyCells:
    def test_single_cell_stats(self) -> None:
        cells = identify_cells(_field([(90.0, 50_000.0, 3.0, 4_000.0, 55.0)]), 35.0, 5.0)
        assert len(cells) == 1
        c = cells[0]
        assert c.id == "C001"
        assert c.centroid_az == pytest.approx(90.0, abs=0.5)
        assert c.centroid_range == pytest.approx(50_000.0, rel=0.02)
        assert 35.0 <= c.mean_dbz <= c.max_dbz <= 55.0
        assert c.area_km2 > 5.0
        assert 0 < len(c.boundary_points) <= 120

    def test_sorted_by_max_dbz_and_min_size(self) -> None:
        data = _field(
            [
                (30.0, 50_000.0, 3.0, 4_000.0, 50.0),
                (200.0, 80_000.0, 3.0, 4_000.0, 65.0),
                (300.0, 20_000.0, 0.4, 300.0, 70.0),  # tiny
            ]
        )
        cells = identify_cells(data, 35.0, 10.0)
        assert len(cells) == 2
        assert cells[0].max_dbz > cells[1].max_dbz > 45.0
        assert cells[0].centroid_az == pytest.approx(200.0, abs=1.0)
        assert [c.id for c in cells] == ["C001", "C002"]

    def test_empty_and_all_nan(self) -> None:
        data = _field([(90.0, 50_000.0, 3.0, 4_000.0, 20.0)])
        assert identify_cells(data, 35.0, 1.0) == []
        data = data.copy(data=np.full(data.shape, np.nan, dtype=np.float32))
        assert identify_cells(data, 35.0, 1.0) == []

    def test_many_cells_stats_match_per_label_reference(self) -> None:
        rng = np.random.default_rng(0)
        blobs = [
            (rng.uniform(20, 340), rng.uniform(20e3, 90e3), 2.5, 3_000.0, rng.uniform(45, 70))
            for _ in range(15)
        ]
        data = _field(blobs)
        cells = identify_cells(data, 35.0, 1.0)
        from scipy import ndimage

        labels, n = ndimage.label(data.values >= 35.0, structure=np.ones((3, 3)))
        ref = sorted(
            (float(data.values[labels == i].max()), float(data.values[labels == i].mean()))
            for i in range(1, n + 1)
        )
        got = sorted((c.max_dbz, c.mean_dbz) for c in cells)
        assert len(got) == len(ref)
        for (gm, gmean), (rm, rmean) in zip(got, ref, strict=True):
            assert gm == pytest.approx(rm, abs=1e-3)
            assert gmean == pytest.approx(rmean, abs=1e-3)


class TestSeam:
    @pytest.mark.parametrize("centre", [0.0, 359.5, 0.5, 1.0])
    def test_cell_across_north_is_one_cell(self, centre: float) -> None:
        cells = identify_cells(_field([(centre, 50_000.0, 4.0, 4_000.0, 55.0)]), 35.0, 5.0)
        assert len(cells) == 1
        c = cells[0]
        diff = (c.centroid_az - centre + 180.0) % 360.0 - 180.0
        assert abs(diff) < 0.5, f"centroid {c.centroid_az} should be near {centre}"
        # Circular mean: must not be dragged to ~180 by a plain average of 359 and 1
        assert c.centroid_range == pytest.approx(50_000.0, rel=0.02)
        azs = np.array([p[0] for p in c.boundary_points])
        assert (azs < 10).any() and (azs > 350).any()  # boundary spans both sides

    def test_seam_stats_combine(self) -> None:
        """Area/max of the merged cell equal those of the same blob rotated away from the seam."""
        on_seam = identify_cells(_field([(0.0, 50_000.0, 4.0, 4_000.0, 55.0)]), 35.0, 5.0)[0]
        off_seam = identify_cells(_field([(180.0, 50_000.0, 4.0, 4_000.0, 55.0)]), 35.0, 5.0)[0]
        assert on_seam.area_km2 == pytest.approx(off_seam.area_km2, rel=1e-6)
        assert on_seam.max_dbz == pytest.approx(off_seam.max_dbz)
        assert on_seam.mean_dbz == pytest.approx(off_seam.mean_dbz)
        assert on_seam.centroid_range == pytest.approx(off_seam.centroid_range, rel=1e-6)

    def test_seam_boundary_has_no_artificial_edge(self) -> None:
        """The seam row must not be reported as boundary for a merged cell."""
        c = identify_cells(_field([(0.0, 50_000.0, 6.0, 6_000.0, 60.0)]), 35.0, 5.0)[0]
        near_seam = [p for p in c.boundary_points if p[0] in (0.5, 359.5)]
        # Only the tips where the blob's range-edges cross the seam can be boundary.
        ranges = sorted(p[1] for p in near_seam)
        interior = [r for r in ranges if ranges[0] < r < ranges[-1]]
        assert not interior

    def test_unsorted_scan_order_across_seam(self) -> None:
        """Azimuths rolled so the scan starts mid-circle (as real data does)."""
        az = np.roll(np.arange(N_AZ) + 0.5, -137)
        cells = identify_cells(_field([(0.0, 50_000.0, 4.0, 4_000.0, 55.0)], az=az), 35.0, 5.0)
        assert len(cells) == 1
        diff = (cells[0].centroid_az + 180.0) % 360.0 - 180.0
        assert abs(diff) < 0.5

    def test_sector_scan_not_wrapped(self) -> None:
        """A sector scan whose ends are far apart must not merge first/last rows."""
        az = np.arange(0.5, 90.0, 1.0)
        vals = np.zeros((az.size, RANGE_M.size), dtype=np.float32)
        vals[0:3, 100:130] = 50.0
        vals[-3:, 100:130] = 50.0
        data = xr.DataArray(
            vals, dims=("azimuth", "range"), coords={"azimuth": az, "range": RANGE_M}
        )
        assert len(identify_cells(data, 35.0, 0.0)) == 2


def _cell(cid: str, az: float, rng: float) -> CellInfo:
    return CellInfo(cid, az, rng, 50.0, 50.0, 45.0, [])


class TestTracking:
    def test_match_across_seam(self) -> None:
        t0 = [_cell("C001", 359.0, 50_000.0)]
        t1 = [_cell("C001", 1.0, 50_000.0)]
        tracks = track_cells(t0, t1, max_distance_km=10.0, dt_seconds=300.0)
        assert len(tracks) == 1 and len(tracks[0].cell_history) == 2
        # 2 deg at 50 km ~ 1.75 km in 300 s, moving east (to) -> wind from the west (270)
        assert tracks[0].speed_mps == pytest.approx(1745.0 / 300.0, rel=0.01)
        assert tracks[0].direction_deg == pytest.approx(270.0, abs=0.5)

    def test_global_nearest_assignment(self) -> None:
        """c0_a (listed first) is farther from the shared neighbour than c0_b is."""
        t0 = [_cell("C001", 90.0, 60_000.0), _cell("C002", 90.0, 50_000.0)]
        t1 = [_cell("C001", 90.0, 51_000.0)]
        tracks = track_cells(t0, t1, max_distance_km=30.0)
        by_id = {t.track_id: t for t in tracks}
        assert len(by_id["T002"].cell_history) == 2  # nearest wins
        assert len(by_id["T001"].cell_history) == 1

    def test_unmatched_become_new_tracks(self) -> None:
        tracks = track_cells([], [_cell("C001", 10.0, 10_000.0)])
        assert [t.track_id for t in tracks] == ["TN01"]

    @pytest.mark.parametrize("dt", [0.0, -5.0, float("nan")])
    def test_bad_dt_gives_zero_motion(self, dt: float) -> None:
        assert _compute_motion(_cell("C001", 0, 1000), _cell("C001", 10, 1000), dt) == (0.0, 0.0)

    def test_stationary_has_zero_motion(self) -> None:
        c = _cell("C001", 45.0, 20_000.0)
        assert _compute_motion(c, c, 300.0) == (0.0, 0.0)

    @pytest.mark.parametrize(
        "az,r0,r1,expected_from",
        [
            (0.0, 10_000.0, 11_000.0, 180.0),  # moving north -> wind from the south
            (90.0, 10_000.0, 11_000.0, 270.0),  # moving east -> wind from the west
            (0.0, 10_000.0, 9_000.0, 0.0),  # moving south -> wind from the north
        ],
    )
    def test_direction_is_from_convention(
        self, az: float, r0: float, r1: float, expected_from: float
    ) -> None:
        _speed, d = _compute_motion(_cell("C001", az, r0), _cell("C001", az, r1), 100.0)
        assert d == pytest.approx(expected_from, abs=1e-6)

    def test_extrapolate_roundtrip(self) -> None:
        c0, c1 = _cell("C001", 350.0, 40_000.0), _cell("C001", 355.0, 41_000.0)
        speed, d = _compute_motion(c0, c1, 300.0)
        az, rng = extrapolate_cell(c0, speed, d, 300.0)
        assert az == pytest.approx(c1.centroid_az, abs=1e-6)
        assert rng == pytest.approx(c1.centroid_range, rel=1e-9)

    def test_extrapolate_past_origin_wraps(self) -> None:
        az, rng = extrapolate_cell(_cell("C001", 0.0, 1_000.0), 10.0, 0.0, 300.0)
        # heading south (from north) 3 km from 1 km north of radar -> 2 km south
        assert az == pytest.approx(180.0, abs=1e-6)
        assert rng == pytest.approx(2_000.0, rel=1e-9)


class TestSharedHelpers:
    """The pieces engine.cells builds on (they must keep the numpy fallback working alone)."""

    def test_labelled_identify_matches_plain_identify(self) -> None:
        data = _field([(30.0, 50_000.0, 3.0, 4_000.0, 50.0), (200.0, 80_000.0, 3.0, 4_000.0, 65.0)])
        cells, labels = identify_cells_labelled(data, 35.0, 5.0)
        assert [c.to_dict() for c in cells] == [
            c.to_dict() for c in identify_cells(data, 35.0, 5.0)
        ]
        assert labels.shape == data.shape and labels.dtype == np.int32
        for k, c in enumerate(cells, start=1):  # label k <-> cells[k - 1]
            assert float(data.values[labels == k].max()) == pytest.approx(c.max_dbz)
        assert not (labels > 0).ravel()[np.isnan(data.values).ravel()].any()

    def test_seam_cell_has_one_label(self) -> None:
        field = _field([(0.0, 50_000.0, 4.0, 4_000.0, 55.0)])
        cells, labels = identify_cells_labelled(field, 35.0, 5.0)
        assert len(cells) == 1 and set(np.unique(labels)) == {0, 1}
        assert labels[0].any() and labels[-1].any()

    def test_cells_from_labels_filters_and_ranks(self) -> None:
        data = _field([(30.0, 50_000.0, 3.0, 4_000.0, 50.0), (200.0, 80_000.0, 3.0, 4_000.0, 65.0)])
        values = data.values
        az = data.azimuth.values
        labels = np.zeros(values.shape, dtype=np.int32)
        labels[(values >= 35) & (az[:, None] < 100)] = 1  # the 50 dBZ cell
        labels[(values >= 35) & (az[:, None] > 100)] = 2  # the 65 dBZ cell
        labels[10:12, 5:7] = 3  # a 4-gate speck, far below the minimum area
        cells, ranked = cells_from_labels(values, labels, az, data.range.values, 10.0)
        assert [c.id for c in cells] == ["C001", "C002"]
        assert cells[0].max_dbz > cells[1].max_dbz
        assert set(np.unique(ranked)) == {0, 1, 2} and ranked[10, 5] == 0
        empty = cells_from_labels(values, np.zeros_like(labels), az, data.range.values)
        assert empty[0] == []

    def test_optional_cell_fields_are_additive(self) -> None:
        c = _cell("C001", 10.0, 20_000.0)
        assert set(c.to_dict()) == {
            "id",
            "centroid_az",
            "centroid_range",
            "area_km2",
            "max_dbz",
            "mean_dbz",
            "boundary_points",
        }
        c.lat, c.lon, c.speed_mps, c.direction_deg = 35.123456, -97.5, 12.34, 270.0
        c.track_id, c.lifetime, c.extra = "T001", 3, {"vil_max": 40.0, "bad": float("nan")}
        d = c.to_dict()
        assert d["lat"] == 35.1235 and d["speed_mps"] == 12.34 and d["direction_deg"] == 270.0
        assert d["track_id"] == "T001" and d["lifetime"] == 3 and d["extra"] == {"vil_max": 40.0}

    def test_track_dict_has_new_fields_with_safe_defaults(self) -> None:
        d = CellTrack("T001", [_cell("C001", 1, 1000)]).to_dict()
        assert d["lifetime"] == 1 and d["split"] is False and d["merge"] is False
        assert d["u_mps"] == 0.0 and "status" not in d and "split_from" not in d
        t = CellTrack("T002", [], split_from="T001", merged_into="T003", status="ended")
        d = t.to_dict()
        assert d["split_from"] == "T001" and d["merged_into"] == "T003" and d["status"] == "ended"

    def test_match_nearest_is_global_and_bounded(self) -> None:
        t0 = [_cell("C001", 90.0, 60_000.0), _cell("C002", 90.0, 50_000.0)]
        t1 = [_cell("C001", 90.0, 51_000.0), _cell("C002", 270.0, 50_000.0)]
        assert match_nearest(t0, t1, 30.0) == {1: 0}
        assert match_nearest(t0, t1, 0.5) == {}
        assert match_nearest([], t1) == {}

    def test_geo_helpers(self) -> None:
        lat, lon = polar_to_latlon(35.0, -97.0, 0.0, 111_195.0)  # due north, one degree
        assert lat == pytest.approx(36.0, abs=0.01) and lon == pytest.approx(-97.0, abs=1e-6)
        lat, lon = polar_to_latlon(0.0, 179.9, 90.0, 50_000.0)  # across the date line
        assert lon < -179.0 and lat == pytest.approx(0.0, abs=1e-3)
        assert beam_height_km(100_000.0, 0.5) == pytest.approx(1.46, abs=0.05)
        assert beam_height_km(0.0, 5.0) == pytest.approx(0.0, abs=1e-6)

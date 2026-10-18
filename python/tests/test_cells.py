"""Tests for engine.cells — the unified (radarx / numpy) cell identification, tracking, stats.

Every behavioural test runs on both backends; the ``radarx`` parametrisation is skipped when the
installed radarx has no ``identify_cells`` / ``track_cells`` / ``cell_statistics``.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

import server
from engine import cells as C  # noqa: N812
from engine import plugins
from engine.dualpol import _get_sweep_ds
from engine.reader import RadarReader
from engine.tracking import CellInfo

HAS_RADARX = C.radarx_retrieve() is not None
needs_radarx = pytest.mark.skipif(not HAS_RADARX, reason="radarx with cell tracking not installed")
BACKEND_PARAMS = [
    "numpy",
    pytest.param("radarx", marks=needs_radarx),
]

AZ = np.arange(360) + 0.5
RNG = 250.0 + 250.0 * np.arange(400)  # 0.25 .. 100 km
DT = 300.0


def make_field(
    blobs: list[tuple[float, float, float, float]],
    *,
    azimuth: np.ndarray = AZ,
    time0: str | None = None,
) -> xr.DataArray:
    """Gaussian blobs in x/y: (x_m east, y_m north, sigma_m, peak_dbz); background 0 dBZ."""
    a, r = np.meshgrid(np.radians(azimuth), RNG, indexing="ij")
    x, y = r * np.sin(a), r * np.cos(a)
    f = np.zeros(a.shape)
    for bx, by, s, peak in blobs:
        f = np.maximum(f, peak * np.exp(-(((x - bx) ** 2 + (y - by) ** 2) / s**2)))
    coords: dict = {"azimuth": azimuth, "range": RNG}
    if time0 is not None:
        t = np.datetime64(time0, "ns") + np.zeros(azimuth.size, dtype="timedelta64[ns]")
        coords["time"] = ("azimuth", t)
    return xr.DataArray(f.astype(np.float32), dims=("azimuth", "range"), coords=coords)


def moving(
    k: int, x0: float, y0: float, u: float, v: float, sigma: float = 7e3, peak: float = 55.0
):
    return (x0 + u * DT * k, y0 + v * DT * k, sigma, peak)


def ang_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def from_dir(u: float, v: float) -> float:
    return (math.degrees(math.atan2(u, v)) + 180.0) % 360.0


@pytest.fixture(autouse=True)
def _fresh_backend_cache():
    """Some tests simulate a missing radarx; always restore the real detection."""
    saved = dict(C._RX_CACHE)
    yield
    C._RX_CACHE.clear()
    C._RX_CACHE.update(saved)


# ---------------------------------------------------------------------------
# Backend selection / fallback
# ---------------------------------------------------------------------------


class TestBackendSelection:
    def test_numpy_is_always_available(self) -> None:
        assert C.resolve_backend("numpy") == "numpy"
        assert C.backend_label("numpy").startswith("numpy")

    def test_bad_backend_name(self) -> None:
        with pytest.raises(ValueError, match="backend"):
            C.resolve_backend("bogus")

    def test_missing_radarx_falls_back(self) -> None:
        C._RX_CACHE["mod"] = None  # as if radarx were not installed (or too old)
        assert C.resolve_backend("auto") == "numpy"
        scan = C.identify(make_field([(20e3, 30e3, 7e3, 55.0)]), 35.0, 10.0)
        assert scan.backend == "numpy" and len(scan) == 1 and scan.note == ""
        # asking for radarx explicitly still works, and says why it did not run
        scan = C.identify(make_field([(20e3, 30e3, 7e3, 55.0)]), 35.0, 10.0, backend="radarx")
        assert scan.backend == "numpy" and "radarx is not installed" in scan.note
        tr = C.track([scan, scan], dt_seconds=DT, backend="radarx")
        assert tr.backend == "numpy" and "not installed" in tr.note

    @needs_radarx
    def test_radarx_failure_falls_back(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("kaput")

        monkeypatch.setattr(C, "_identify_radarx", boom)
        scan = C.identify(make_field([(20e3, 30e3, 7e3, 55.0)]), 35.0, 10.0)
        assert scan.backend == "numpy" and "kaput" in scan.note and len(scan) == 1

    @needs_radarx
    def test_auto_prefers_radarx(self) -> None:
        scan = C.identify(make_field([(20e3, 30e3, 7e3, 55.0)]), 35.0, 10.0)
        assert scan.backend == "radarx" and "radarx" in scan.backend_label


# ---------------------------------------------------------------------------
# identify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKEND_PARAMS)
class TestIdentify:
    def test_single_cell_stats(self, backend: str) -> None:
        x, y = 30e3, 40e3
        scan = C.identify(
            make_field([(x, y, 7e3, 55.0)]),
            35.0,
            10.0,
            backend=backend,
            lat=35.0,
            lon=-97.0,
            elevation_deg=0.5,
        )
        assert scan.backend == backend and len(scan) == 1
        c = scan[0]
        assert c.id == "C001"
        assert c.centroid_range == pytest.approx(math.hypot(x, y), rel=0.02)
        assert ang_diff(c.centroid_az, math.degrees(math.atan2(x, y))) < 0.7
        assert c.max_dbz == pytest.approx(55.0, abs=0.5)
        # area of the >=35 dBZ disc: pi r^2 with r = sigma sqrt(ln(55/35))
        expected = math.pi * (7.0**2) * math.log(55 / 35)
        assert c.area_km2 == pytest.approx(expected, rel=0.12)
        # geo-reference: north-east of the radar
        assert c.lat > 35.0 and c.lon > -97.0
        assert c.echo_top_proxy_km is not None and c.echo_top_proxy_km > 0
        # polar label image: value k <-> cells[k-1]
        assert scan.labels.max() == 1 and (scan.labels > 0).sum() > 100

    def test_threshold_and_min_area(self, backend: str) -> None:
        field = make_field(
            [(30e3, 40e3, 7e3, 55.0), (-40e3, -20e3, 1.2e3, 60.0), (-30e3, 50e3, 9e3, 33.0)]
        )
        assert len(C.identify(field, 35.0, 10.0, backend=backend)) == 1  # tiny + weak dropped
        assert len(C.identify(field, 35.0, 0.0, backend=backend)) == 2  # tiny one is back
        assert len(C.identify(field, 70.0, 0.0, backend=backend)) == 0
        assert len(C.identify(field, 30.0, 10.0, backend=backend)) == 2  # weak one is big enough

    def test_ranking_by_peak(self, backend: str) -> None:
        field = make_field([(30e3, 40e3, 7e3, 50.0), (-40e3, 20e3, 7e3, 65.0)])
        scan = C.identify(field, 35.0, 10.0, backend=backend)
        assert [c.id for c in scan] == ["C001", "C002"]
        assert scan[0].max_dbz > scan[1].max_dbz
        # labels follow the ranking
        a = scan.labels == 1
        assert a.any() and scan[0].max_dbz == pytest.approx(float(field.values[a].max()), abs=0.01)

    def test_all_nan_and_empty(self, backend: str) -> None:
        field = make_field([(30e3, 40e3, 7e3, 55.0)])
        nan = field.copy(data=np.full(field.shape, np.nan, dtype=np.float32))
        scan = C.identify(nan, 35.0, 1.0, backend=backend)
        assert len(scan) == 0 and scan.backend == backend
        assert C.cell_rows(scan) == []

    @pytest.mark.parametrize("centre_x", [0.0, 2e3, -2e3])
    def test_cell_across_north_is_one_cell(self, backend: str, centre_x: float) -> None:
        scan = C.identify(make_field([(centre_x, 40e3, 7e3, 55.0)]), 35.0, 10.0, backend=backend)
        assert len(scan) == 1
        c = scan[0]
        assert ang_diff(c.centroid_az, math.degrees(math.atan2(centre_x, 40e3))) < 0.7
        assert c.centroid_range == pytest.approx(math.hypot(centre_x, 40e3), rel=0.02)
        azs = np.array([p[0] for p in c.boundary_points])
        assert (azs < 15).any() and (azs > 345).any()  # outline spans both sides of the seam

    def test_seam_cell_matches_rotated_copy(self, backend: str) -> None:
        on = C.identify(make_field([(0.0, 40e3, 7e3, 55.0)]), 35.0, 10.0, backend=backend)[0]
        off = C.identify(make_field([(40e3, 0.0, 7e3, 55.0)]), 35.0, 10.0, backend=backend)[0]
        assert on.area_km2 == pytest.approx(off.area_km2, rel=0.03)
        assert on.max_dbz == pytest.approx(off.max_dbz, abs=0.2)

    def test_sector_scan_not_wrapped(self, backend: str) -> None:
        """A 90 degree sector whose first and last rays are far apart: two cells, not one."""
        az = np.arange(0.5, 90.0, 1.0)
        vals = np.zeros((az.size, RNG.size), dtype=np.float32)
        vals[0:4, 100:130] = 50.0
        vals[-4:, 100:130] = 50.0
        da = xr.DataArray(vals, dims=("azimuth", "range"), coords={"azimuth": az, "range": RNG})
        assert len(C.identify(da, 35.0, 0.0, backend=backend)) == 2

    def test_rolled_azimuth_order(self, backend: str) -> None:
        """Rays stored from mid-circle (as real files do) still give one seam cell."""
        az = np.roll(AZ, -137)
        field = make_field([(0.0, 40e3, 7e3, 55.0)], azimuth=az)
        scan = C.identify(field, 35.0, 10.0, backend=backend)
        assert len(scan) == 1 and ang_diff(scan[0].centroid_az, 0.0) < 0.7

    def test_extra_fields_per_cell(self, backend: str) -> None:
        field = make_field([(30e3, 40e3, 7e3, 55.0), (-40e3, 20e3, 7e3, 65.0)])
        extra = np.where(field.values >= 35.0, 10.0, np.nan)
        extra[field.values >= 60.0] = 25.0
        scan = C.identify(field, 35.0, 10.0, backend=backend, extra_fields={"vil": extra})
        by_peak = {round(c.max_dbz): c for c in scan}
        strong, weak = by_peak[65], by_peak[55]
        assert strong.extra["vil_max"] == pytest.approx(25.0)
        assert weak.extra["vil_max"] == pytest.approx(10.0)
        assert 10.0 < strong.extra["vil_mean"] < 25.0
        rows = C.cell_rows(scan)
        assert {"vil_max_kgm2", "vil_mean_kgm2"} <= set(rows[0])

    def test_scan_time_kept(self, backend: str) -> None:
        scan = C.identify(make_field([(30e3, 40e3, 7e3, 55.0)]), backend=backend, time_s=1234.5)
        assert scan.time_s == 1234.5


@needs_radarx
class TestMultiThreshold:
    def test_touching_cores_split(self) -> None:
        """Two 60 dBZ cores joined by a ~42 dBZ saddle: one cell at 35 dBZ, two with thresholds."""
        field = make_field([(-3e3, 40e3, 5e3, 60.0), (3e3, 40e3, 5e3, 60.0)])
        assert len(C.identify(field, 35.0, 5.0, backend="radarx")) == 1
        scan = C.identify(field, 35.0, 5.0, thresholds=[35.0, 50.0], backend="radarx")
        assert len(scan) == 2
        # together they still cover the whole 35 dBZ region
        whole = C.identify(field, 35.0, 5.0, backend="radarx")[0]
        assert sum(c.area_km2 for c in scan) == pytest.approx(whole.area_km2, rel=0.01)

    def test_numpy_ignores_extra_thresholds_and_says_so(self) -> None:
        field = make_field([(-3e3, 40e3, 5e3, 60.0), (3e3, 40e3, 5e3, 60.0)])
        scan = C.identify(field, 35.0, 5.0, thresholds=[35.0, 50.0], backend="numpy")
        assert len(scan) == 1 and "ignores extra thresholds" in scan.note


# ---------------------------------------------------------------------------
# tracking
# ---------------------------------------------------------------------------


def _scans(
    backend: str,
    blob_fn,
    n: int = 4,
    *,
    lat: float | None = None,
    lon: float | None = None,
) -> list[C.CellScan]:
    return [
        C.identify(
            make_field(blob_fn(k)), 35.0, 10.0, backend=backend, time_s=k * DT, lat=lat, lon=lon
        )
        for k in range(n)
    ]


@pytest.mark.parametrize("backend", BACKEND_PARAMS)
class TestTrack:
    @pytest.mark.parametrize(
        "u,v", [(15.0, 5.0), (-12.0, 8.0), (0.0, -18.0), (-9.0, -9.0), (20.0, 0.0)]
    )
    def test_recovers_known_motion(self, backend: str, u: float, v: float) -> None:
        scans = _scans(
            backend,
            lambda k: [moving(k, 20e3, 30e3, u, v), (-45e3, -35e3, 6e3, 50.0)],
        )
        res = C.track(scans, backend=backend)
        assert res.backend == backend
        moving_tracks = [t for t in res if t.speed_mps > 1.0]
        assert len(res) == 2 and len(moving_tracks) == 1
        t = moving_tracks[0]
        assert t.lifetime == 4 and len(t.cell_history) == 4 and t.status == "continued"
        assert t.speed_mps == pytest.approx(math.hypot(u, v), rel=0.06)
        assert ang_diff(t.direction_deg, from_dir(u, v)) < 4.0
        assert t.u_mps == pytest.approx(u, abs=1.2) and t.v_mps == pytest.approx(v, abs=1.2)
        # annotated cells carry the motion
        last = t.cell_history[-1]
        assert last.track_id == t.track_id and last.lifetime == 4
        assert last.speed_mps == pytest.approx(t.speed_mps)
        # the stationary storm is tracked too, at ~zero speed
        still = next(x for x in res if x is not t)
        assert still.speed_mps < 1.0 and still.lifetime == 4

    def test_motion_across_north_seam(self, backend: str) -> None:
        u = 15.0
        scans = _scans(backend, lambda k: [moving(k, -7e3, 40e3, u, 0.0)])
        res = C.track(scans, backend=backend)
        assert len(res) == 1 and res[0].lifetime == 4
        assert res[0].speed_mps == pytest.approx(u, rel=0.06)
        assert ang_diff(res[0].direction_deg, 270.0) < 4.0  # eastward -> from the west

    def test_new_and_ended_cells(self, backend: str) -> None:
        def blobs(k: int):
            out = [moving(k, 20e3, 30e3, 10.0, 0.0)]
            if k == 0:
                out.append((-40e3, 20e3, 6e3, 55.0))  # dies after the first scan
            if k >= 2:
                out.append((-30e3, -50e3, 6e3, 55.0))  # appears in the third
            return out

        scans = _scans(backend, blobs)
        res = C.track(scans, backend=backend)
        assert sorted(t.lifetime for t in res) == [1, 2, 4]
        ended = next(t for t in res if t.lifetime == 1)
        assert ended.status == "ended" and ended.cell_history[0] in scans[0]
        born = next(t for t in res if t.lifetime == 2)
        assert born.cell_history[0] in scans[2] and born.status == "continued"
        assert all(not t.split_from and not t.merged_into for t in res)

    def test_single_scan_and_empty(self, backend: str) -> None:
        scan = C.identify(make_field([(20e3, 30e3, 7e3, 55.0)]), 35.0, 10.0, backend=backend)
        res = C.track([scan], backend=backend)
        assert len(res) == 1 and res[0].status == "new" and res[0].speed_mps == 0.0
        assert len(C.track([], backend=backend)) == 0
        empty = C.identify(make_field([]), 35.0, 10.0, backend=backend)
        assert len(C.track([empty, empty], dt_seconds=DT, backend=backend)) == 0

    def test_fixed_dt_without_times(self, backend: str) -> None:
        scans = _scans(backend, lambda k: [moving(k, 20e3, 30e3, 15.0, 0.0)])
        for s in scans:
            s.time_s = None
        res = C.track(scans, dt_seconds=DT, backend=backend)
        assert res[0].speed_mps == pytest.approx(15.0, rel=0.06)
        res = C.track(scans, dt_seconds=[DT, DT, DT], backend=backend)
        assert res[0].speed_mps == pytest.approx(15.0, rel=0.06)
        res = C.track(scans, times_s=[0, DT, 2 * DT, 3 * DT], backend=backend)
        assert res[0].speed_mps == pytest.approx(15.0, rel=0.06)
        with pytest.raises(ValueError):
            C.track(scans, times_s=[0, 1], backend=backend)

    def test_bad_dt_gives_zero_motion(self, backend: str) -> None:
        scans = _scans(backend, lambda k: [moving(k, 20e3, 30e3, 15.0, 0.0)], n=2)
        for dt in (0.0, -5.0, float("nan")):
            res = C.track_pair(scans[0], scans[1], dt_seconds=dt, backend=backend)
            assert len(res) == 1 and res[0].speed_mps == 0.0 and res[0].lifetime == 2

    def test_cells_beyond_max_distance_are_not_linked(self, backend: str) -> None:
        a = C.identify(make_field([(-40e3, 40e3, 7e3, 55.0)]), 35.0, 10.0, backend=backend)
        b = C.identify(make_field([(40e3, -40e3, 7e3, 55.0)]), 35.0, 10.0, backend=backend)
        res = C.track([a, b], dt_seconds=DT, max_distance_km=30.0, backend=backend)
        assert sorted(t.lifetime for t in res) == [1, 1]

    def test_plain_cell_lists_are_tracked_by_outline(self, backend: str) -> None:
        """No labels / geometry (e.g. cells built by hand or deserialised): outlines are used."""
        scans = _scans(backend, lambda k: [moving(k, 20e3, 30e3, 15.0, 5.0)])
        plain = [list(s) for s in scans]
        res = C.track(plain, times_s=[0, DT, 2 * DT, 3 * DT], backend=backend)
        assert len(res) == 1 and res[0].lifetime == 4
        assert res[0].speed_mps == pytest.approx(math.hypot(15, 5), rel=0.08)


class TestTrackPairLegacyShape:
    @pytest.mark.parametrize("backend", BACKEND_PARAMS)
    def test_ids_and_dict_shape(self, backend: str) -> None:
        t0 = C.identify(
            make_field([(20e3, 30e3, 7e3, 60.0), (-40e3, 20e3, 7e3, 50.0)]), backend=backend
        )
        t1 = C.identify(
            make_field(
                [(24.5e3, 30e3, 7e3, 60.0), (-40e3, -50e3, 7e3, 50.0), (60e3, 10e3, 7e3, 45.0)]
            ),
            backend=backend,
        )
        res = C.track_pair(t0, t1, dt_seconds=DT, backend=backend)
        ids = [t.track_id for t in res]
        assert ids[0] == "T001"  # C001 continued
        assert "T002" in ids  # C002 found no successor (moved > 30 km): kept its id
        assert {i for i in ids if i.startswith("TN")} == {"TN02", "TN03"}
        d = res[0].to_dict()
        assert {"track_id", "cell_history", "speed_mps", "direction_deg"} <= set(d)
        assert {"u_mps", "v_mps", "lifetime", "split", "merge", "status"} <= set(d)
        assert len(d["cell_history"]) == 2 and d["speed_mps"] == pytest.approx(4.5e3 / DT, rel=0.1)
        json.dumps(d, allow_nan=False)


@needs_radarx
class TestSplitMerge:
    def _wide(self, x: float = 0.0):
        return [(x, 40e3, 14e3, 55.0)]

    def _pair(self, x: float = 0.0):
        return [(x - 9e3, 40e3, 7e3, 55.0), (x + 9e3, 40e3, 7e3, 55.0)]

    def _run(self, blobs0, blobs1):
        scans = [
            C.identify(make_field(b), 35.0, 5.0, backend="radarx", time_s=i * DT)
            for i, b in enumerate((blobs0, blobs1))
        ]
        return scans, C.track(scans, backend="radarx", min_overlap=0.3, max_distance_km=0.0001)

    def test_split(self) -> None:
        scans, res = self._run(self._wide(), self._pair())
        assert len(scans[0]) == 1 and len(scans[1]) == 2
        assert len(res) == 2
        parent = next(t for t in res if t.cell_history[0] in scans[0])
        child = next(t for t in res if t is not parent)
        assert parent.lifetime == 2 and parent.split_from is None
        assert child.lifetime == 1 and child.split_from == parent.track_id
        assert child.to_dict()["split"] is True and child.to_dict()["split_from"] == "T001"
        assert child.status == "new"

    def test_merge(self) -> None:
        scans, res = self._run(self._pair(), self._wide())
        assert len(scans[0]) == 2 and len(scans[1]) == 1
        assert len(res) == 2
        ended = next(t for t in res if t.status == "ended")
        survivor = next(t for t in res if t is not ended)
        assert survivor.lifetime == 2 and ended.lifetime == 1
        assert ended.merged_into == survivor.track_id
        assert ended.to_dict()["merge"] is True

    def test_numpy_reports_no_split_merge(self) -> None:
        scans = [
            C.identify(make_field(b), 35.0, 5.0, backend="numpy", time_s=i * DT)
            for i, b in enumerate((self._wide(), self._pair()))
        ]
        res = C.track(scans, backend="numpy")
        assert all(t.split_from is None and t.merged_into is None for t in res)


@needs_radarx
class TestRadarxMotionEstimate:
    def test_advection_links_fast_cells(self) -> None:
        """A cell that moves two diameters per scan has no overlap; motion makes it link."""
        u = 60.0  # 18 km per 300 s, cell diameter ~ 9 km
        scans = _scans("radarx", lambda k: [moving(k, -30e3, 30e3, u, 0.0, sigma=5e3)])
        res = C.track(scans, backend="radarx", max_distance_km=0.0001, motion=(u, 0.0))
        assert len(res) == 1 and res[0].lifetime == 4
        assert res[0].speed_mps == pytest.approx(u, rel=0.06)
        assert res.motion == (u, 0.0)

    def test_estimated_motion_is_reported(self) -> None:
        scans = _scans("radarx", lambda k: [moving(k, 20e3, 30e3, 15.0, 5.0)])
        res = C.track(scans, backend="radarx")
        assert res.motion is not None
        assert res.motion[0] == pytest.approx(15.0, abs=2.0)
        assert res.motion[1] == pytest.approx(5.0, abs=2.0)


# ---------------------------------------------------------------------------
# helpers: params, table, overlay
# ---------------------------------------------------------------------------


class TestOptionsAndRows:
    def test_options_defaults_and_validation(self) -> None:
        assert C.options_from_params({}) == {"backend": "auto"}
        opts = C.options_from_params(
            {"backend": "numpy", "thresholds": [35, 45], "grid_spacing_km": 1, "smooth": 0.5}
        )
        assert opts == {
            "backend": "numpy",
            "thresholds": [35.0, 45.0],
            "grid_spacing_km": 1.0,
            "smooth": 0.5,
        }
        for bad in (
            {"backend": "gpu"},
            {"thresholds": []},
            {"thresholds": ["a"]},
            {"grid_spacing_km": -1},
            {"smooth": True},
        ):
            with pytest.raises(ValueError):
                C.options_from_params(bad)

    def test_rows_and_columns_only_include_known_fields(self) -> None:
        plain = C.identify(make_field([(20e3, 30e3, 7e3, 55.0)]), backend="numpy")
        rows = C.cell_rows(plain)
        assert set(rows[0]) == {
            "id",
            "centroid_az",
            "centroid_range_km",
            "area_km2",
            "max_dbz",
            "mean_dbz",
        }
        assert [c["key"] for c in C.table_columns(rows)] == list(rows[0])
        rich = C.identify(
            make_field([(20e3, 30e3, 7e3, 55.0)]),
            backend="numpy",
            lat=35.0,
            lon=-97.0,
            elevation_deg=0.5,
        )
        C.set_motion(rich, 12.0, 270.0)
        rows = C.cell_rows(rich)
        assert {"lat", "lon", "echo_top_proxy_km", "speed_ms", "direction_deg"} <= set(rows[0])
        keys = {c["key"] for c in C.table_columns(rows)}
        assert keys == set(rows[0])
        json.dumps(rows, allow_nan=False)

    def test_arrow_points_downwind(self) -> None:
        c = CellInfo("C001", 0.0, 40_000.0, 50.0, 50.0, 45.0, [])
        assert C.arrow_coords(c) is None  # no motion yet
        C.set_motion([c], 10.0, 270.0)  # moving east
        arrow = C.arrow_coords(c, minutes=10)
        assert arrow is not None and len(arrow) == 5
        tail, head = arrow[0], arrow[1]
        assert tail == [0.0, 40000.0]
        # 10 m/s for 10 min = 6 km east of the cell at (0, 40 km)
        hx = head[1] * math.sin(math.radians(head[0]))
        hy = head[1] * math.cos(math.radians(head[0]))
        assert hx == pytest.approx(6000.0, abs=30.0) and hy == pytest.approx(40_000.0, abs=30.0)
        assert arrow[3] == head  # the two barbs return to the head
        c.speed_mps = 0.0
        assert C.arrow_coords(c) is None

    def test_site_and_time_helpers(self) -> None:
        tree = xr.DataTree.from_dict(
            {"/": xr.Dataset(attrs={"latitude": 35.3, "longitude": -97.5})}
        )
        assert C.site_lat_lon(tree) == (35.3, -97.5)
        assert C.site_lat_lon(xr.DataTree()) == (None, None)
        da = make_field([(20e3, 30e3, 7e3, 55.0)], time0="2024-05-01T12:00:00")
        assert C.scan_time_seconds(da) == pytest.approx(1714564800.0)
        assert C.scan_time_seconds(make_field([])) is None
        el = xr.Dataset(coords={"elevation": ("azimuth", np.full(4, 1.5))})
        assert C.dataset_elevation_deg(el) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# the storm_detection module
# ---------------------------------------------------------------------------


def _storm_tree(blobs) -> xr.DataTree:
    ds = make_field(blobs).to_dataset(name="DBZH")
    ds = ds.assign_coords(elevation=("azimuth", np.full(AZ.size, 0.5, dtype=np.float32)))
    root = xr.Dataset(attrs={"latitude": 35.33, "longitude": -97.28})
    return xr.DataTree.from_dict({"/": root, "/sweep_0": ds})


def _run_module(tree: xr.DataTree, module_id: str = "storm_detection", **params):
    plugins.discover_modules()
    return plugins.run_module(
        module_id, tree, 0, params, get_sweep_dataset=lambda s: _get_sweep_ds(tree, s)
    )


@pytest.mark.parametrize("backend", BACKEND_PARAMS)
class TestStormDetectionModule:
    def test_table_overlay_and_backend_text(self, backend: str) -> None:
        out = _run_module(
            _storm_tree([(30e3, 40e3, 7e3, 55.0), (-40e3, 20e3, 7e3, 65.0)]), backend=backend
        )
        blocks = {b["type"]: b for b in out["blocks"]}
        assert [b["type"] for b in out["blocks"]] == ["scalars", "table", "overlay"]
        scal = {i["label"]: i["value"] for i in blocks["scalars"]["items"]}
        assert scal["Cells"] == 2
        assert backend in scal["Algorithm"].lower()
        table = blocks["table"]
        keys = {c["key"] for c in table["columns"]}
        assert {"id", "area_km2", "max_dbz", "mean_dbz", "centroid_az", "centroid_range_km"} <= keys
        assert {"lat", "lon", "echo_top_proxy_km"} <= keys  # site + elevation are known
        assert table["id_key"] == "id"
        assert {f["id"] for f in blocks["overlay"]["features"]} == {r["id"] for r in table["rows"]}
        assert all(r["lat"] > 35.33 or r["lon"] < -97.28 for r in table["rows"])
        # nothing is NaN / Inf: the payload must be strict JSON
        json.dumps(out, allow_nan=False)

    def test_motion_adds_arrows_and_speed_column(self, backend: str) -> None:
        out = _run_module(
            _storm_tree([(30e3, 40e3, 7e3, 55.0), (-40e3, 20e3, 7e3, 65.0)]),
            backend=backend,
            motion_speed=15.0,
            motion_from=250.0,
            arrow_minutes=20.0,
        )
        blocks = {b["type"]: b for b in out["blocks"]}
        rows = blocks["table"]["rows"]
        assert all(r["speed_ms"] == 15.0 and r["direction_deg"] == 250 for r in rows)
        feats = blocks["overlay"]["features"]
        arrows = [f for f in feats if f["kind"] == "line"]
        assert sorted(f["id"] for f in arrows) == sorted(f"{r['id']}:motion" for r in rows)
        assert all(len(f["coords"]) == 5 and f["frame"] == "polar" for f in arrows)
        # polygons keep the cell id and now carry the id in the label
        polys = [f for f in feats if f["kind"] == "polygon"]
        assert {f["id"] for f in polys} == {r["id"] for r in rows}
        assert all(f["label"].startswith(f["id"]) for f in polys)

    def test_params_are_backwards_compatible(self, backend: str) -> None:
        tree = _storm_tree([(30e3, 40e3, 7e3, 55.0)])
        out = _run_module(tree, threshold_dbz=35, min_size_km2=10, backend=backend)  # old call
        assert out["params"]["threshold_dbz"] == 35.0 and out["params"]["min_size_km2"] == 10.0
        assert out["params"]["split_cells"] is False and out["params"]["motion_speed"] == 0.0


def test_storm_detection_default_backend_is_reported() -> None:
    out = _run_module(_storm_tree([(30e3, 40e3, 7e3, 55.0)]))
    scal = {i["label"]: i["value"] for i in out["blocks"][0]["items"]}
    expect = "radarx" if HAS_RADARX else "numpy"
    assert expect in scal["Algorithm"]


def test_storm_detection_runs_without_radarx() -> None:
    C._RX_CACHE["mod"] = None
    out = _run_module(_storm_tree([(30e3, 40e3, 7e3, 55.0)]), backend="radarx")
    scal = {i["label"]: i["value"] for i in out["blocks"][0]["items"]}
    assert "numpy" in scal["Algorithm"] and "not installed" in scal["Note"]
    assert scal["Cells"] == 1


def test_cell_size_sort_uses_shared_backend_and_reports_it() -> None:
    out = _run_module(
        _storm_tree([(30e3, 40e3, 7e3, 55.0), (-40e3, 20e3, 7e3, 65.0)]), "cell_size_sort"
    )
    scal = {i["label"]: i["value"] for i in out["blocks"][0]["items"]}
    assert scal["Cells found"] == 2 and "Algorithm" in scal
    rows = out["blocks"][1]["rows"]
    assert [r["rank"] for r in rows] == [1, 2]


# ---------------------------------------------------------------------------
# WebSocket handlers
# ---------------------------------------------------------------------------


def _reader(blobs, time0: str | None = None) -> RadarReader:
    ds = make_field(blobs, time0=time0).to_dataset(name="DBZH")
    ds = ds.assign_coords(elevation=("azimuth", np.full(AZ.size, 0.5, dtype=np.float32)))
    reader = RadarReader()
    reader._dtree = xr.DataTree.from_dict(
        {"/": xr.Dataset(attrs={"latitude": 35.33, "longitude": -97.28}), "/sweep_0": ds}
    )
    reader._format = "synthetic"
    return reader


def _handler():
    from tests.test_server import _make_handler

    return _make_handler()[0]


@pytest.mark.parametrize("backend", BACKEND_PARAMS)
def test_handlers_identify_then_track_keep_the_protocol(backend: str) -> None:
    async def go():
        fid1 = server._register_reader(_reader([moving(0, 20e3, 30e3, 15.0, 5.0)]))
        fid2 = server._register_reader(_reader([moving(1, 20e3, 30e3, 15.0, 5.0)]))
        h = _handler()
        params = {"threshold_dbz": 35, "min_size_km2": 10, "backend": backend}
        await h.handle_message(
            json.dumps(
                {"type": "identify_cells", "variable": "DBZH", "params": params, "file_id": fid1}
            )
        )
        ident = next(m for m in h._sent if m["type"] == "cells_identified")
        await h.handle_message(
            json.dumps(
                {
                    "type": "track_cells",
                    "variable": "DBZH",
                    "params": {**params, "dt_seconds": DT, "max_distance_km": 30},
                    "file_id": fid2,
                }
            )
        )
        return ident, next(m for m in h._sent if m["type"] == "cells_tracked")

    ident, tracked = asyncio.run(go())
    # old response shape is intact ...
    assert {"variable", "sweep", "threshold_dbz", "min_size_km2", "cells"} <= set(ident)
    c = ident["cells"][0]
    assert {"id", "centroid_az", "centroid_range", "area_km2", "max_dbz", "mean_dbz"} <= set(c)
    assert len(c["boundary_points"]) > 2 and all(len(p) == 2 for p in c["boundary_points"])
    assert {"variable", "sweep", "cells", "tracks"} <= set(tracked)
    tr = tracked["tracks"][0]
    assert {"track_id", "cell_history", "speed_mps", "direction_deg"} <= set(tr)
    assert tr["track_id"] == "T001" and len(tr["cell_history"]) == 2
    assert tr["speed_mps"] == pytest.approx(math.hypot(15, 5), rel=0.1)
    # ... and the new fields are additive
    assert ident["backend"] == backend and backend in ident["backend_label"].lower()
    assert "lat" in c and "lon" in c and "echo_top_proxy_km" in c
    assert tracked["backend"] == backend and tracked["dt_seconds"] == DT
    assert tracked["dt_source"] == "request"
    assert tr["lifetime"] == 2 and tr["split"] is False and tr["merge"] is False


def test_handler_dt_from_scan_times_and_bad_option() -> None:
    async def go():
        fid1 = server._register_reader(
            _reader([moving(0, 20e3, 30e3, 15.0, 0.0)], time0="2024-05-01T12:00:00")
        )
        fid2 = server._register_reader(
            _reader([moving(1, 20e3, 30e3, 15.0, 0.0)], time0="2024-05-01T12:05:00")
        )
        h = _handler()
        base = {"threshold_dbz": 35, "min_size_km2": 10, "backend": "numpy"}
        await h.handle_message(
            json.dumps(
                {"type": "identify_cells", "variable": "DBZH", "params": base, "file_id": fid1}
            )
        )
        await h.handle_message(
            json.dumps({"type": "track_cells", "variable": "DBZH", "params": base, "file_id": fid2})
        )
        await h.handle_message(
            json.dumps(
                {
                    "type": "identify_cells",
                    "variable": "DBZH",
                    "params": {"backend": "gpu"},
                    "file_id": fid1,
                }
            )
        )
        return h._sent

    sent = asyncio.run(go())
    tracked = next(m for m in sent if m["type"] == "cells_tracked")
    assert tracked["dt_source"] == "scan times" and tracked["dt_seconds"] == pytest.approx(300.0)
    assert tracked["tracks"][0]["speed_mps"] == pytest.approx(15.0, rel=0.1)
    assert sent[-1]["type"] == "error" and "backend" in sent[-1]["message"]


# ---------------------------------------------------------------------------
# Real data (skipped when the example files are not on this machine)
# ---------------------------------------------------------------------------

KTLX = Path.home() / "Downloads/Others/XRADAR_EXAMPLES/KTLX20130520_201643_V06.gz"
ktlx = pytest.mark.skipif(not KTLX.exists(), reason="KTLX example file not available")


@pytest.fixture(scope="module")
def ktlx_reader() -> RadarReader:
    reader = RadarReader()
    reader.open_file(str(KTLX))
    return reader


@ktlx
@pytest.mark.parametrize("backend", BACKEND_PARAMS)
def test_ktlx_identify_and_self_track(ktlx_reader: RadarReader, backend: str) -> None:
    da = ktlx_reader.get_sweep_data(0, "DBZH")
    scan = C.identify_sweep(ktlx_reader.datatree, da, 35.0, 10.0, backend=backend)
    assert scan.backend == backend and 15 <= len(scan) <= 45
    assert scan[0].max_dbz > 60 and scan[0].lat is not None and 34 < scan[0].lat < 37
    assert scan.time_s is not None
    ref = C.identify_sweep(ktlx_reader.datatree, da, 35.0, 10.0, backend="numpy")
    # the strongest cell is the same one whichever algorithm finds it
    assert scan[0].max_dbz == pytest.approx(ref[0].max_dbz, abs=0.1)
    ax, ay = C._xy_m(scan[0])
    bx, by = C._xy_m(ref[0])
    assert math.hypot(ax - bx, ay - by) < 8e3
    assert sum(c.area_km2 for c in scan) == pytest.approx(sum(c.area_km2 for c in ref), rel=0.25)
    # tracking a scan against itself: nothing moves and every cell continues
    res = C.track_pair(scan, scan, dt_seconds=300.0, backend=backend)
    assert len(res) == len(scan) and all(t.lifetime == 2 for t in res)
    assert max(t.speed_mps for t in res) < 2.0
    # VIL / echo-top columns come from the volume (module path)
    out = _run_module_on_reader(ktlx_reader, backend=backend)
    keys = {c["key"] for c in next(b for b in out["blocks"] if b["type"] == "table")["columns"]}
    assert {"lat", "lon", "echo_top_proxy_km"} <= keys


def _run_module_on_reader(reader: RadarReader, **params):
    plugins.discover_modules()
    return plugins.run_module(
        "storm_detection",
        reader.datatree,
        0,
        {"with_vil": False, **params},
        get_sweep_dataset=reader.get_sweep_dataset,
    )


@ktlx
def test_ktlx_vil_columns(ktlx_reader: RadarReader) -> None:
    pytest.importorskip("radarx.retrieve")
    import radarx.retrieve as rt

    if not hasattr(rt, "vil"):
        pytest.skip("radarx without vil")
    out = plugins.run_module(
        "storm_detection",
        ktlx_reader.datatree,
        0,
        {"with_vil": True},
        get_sweep_dataset=ktlx_reader.get_sweep_dataset,
    )
    table = next(b for b in out["blocks"] if b["type"] == "table")
    assert {"vil_max_kgm2", "vil_mean_kgm2", "echo_top_km"} <= {c["key"] for c in table["columns"]}
    top = table["rows"][0]
    assert 5 < top["vil_max_kgm2"] < 150 and 3 < top["echo_top_km"] < 25
